"""Public source addresses and recorded asking-price events for comparison.

Developer history stores the NEW price after a change, never its first old
price. All events remain visible even when the chain disagrees with the cache.
This module reads no database and never copies contacts or raw source payloads.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
from zoneinfo import ZoneInfo

from bot.core.price_history_integrity import inspect_price_history

LOCAL_TIMEZONE = ZoneInfo("Asia/Almaty")
DETAIL_FIELDS = ("address", "address_note", "building", "section", "price_history")
MAX_EVENTS = 5_000


def _utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("Price observation needs a timezone")
    return value.astimezone(timezone.utc)


def _price(value):
    if isinstance(value, bool):
        raise ValueError("Invalid historical price")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Invalid historical price") from None
    if not math.isfinite(number) or not number.is_integer() or not 0 < number <= 10_000_000_000:
        raise ValueError("Invalid historical price")
    return int(number)


def _text(value, limit):
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError("Invalid source address text")
    return " ".join(value.split()) or None


def _source_address(value):
    if not isinstance(value, str):
        return None
    # The imported field sometimes appends the seller's entire ad after an
    # em dash. Keep the address prefix; never publish that free-form tail.
    prefix = value.replace("–", "—").split("—", 1)[0]
    prefix = (prefix.splitlines() or [""])[0]
    return _text(prefix, 500) if len(prefix) <= 500 else None


def price_history(events, current_price, *, now, baseline_unknown=False):
    """Validate and re-project every event; recompute integrity, never trust a label."""
    if not isinstance(events, list) or len(events) > MAX_EVENTS or not isinstance(baseline_unknown, bool):
        raise ValueError("Invalid price event collection")
    current_price, now = _price(current_price), _utc(now)
    safe, previous_at = [], None
    for index, row in enumerate(events):
        if not isinstance(row, dict):
            raise ValueError("Invalid price event")
        at = _utc(row.get("at"))
        if at > now or (previous_at is not None and at < previous_at):
            raise ValueError("Price events outside observation order")
        old = row.get("old_price")
        if old is None:
            if not (baseline_unknown and index == 0):
                raise ValueError("Unknown previous price")
        else:
            old = _price(old)
        new = _price(row.get("new_price"))
        delta = new - old if old is not None else None
        safe.append({"at": at.isoformat(), "date_label": at.astimezone(LOCAL_TIMEZONE).strftime("%d.%m.%Y, %H:%M"),
                     "old_price": old, "new_price": new, "delta": delta,
                     "delta_pct": round(delta / old * 100, 2) if old else None,
                     "direction": "down" if delta is not None and delta < 0 else "up" if delta is not None and delta > 0 else "unknown"})
        previous_at = at
    if baseline_unknown and safe and safe[0]["old_price"] is not None:
        raise ValueError("Developer first previous price was not recorded")
    # A synthetic equal-price entry checks the first NEW observation against
    # later observations/current price. It is never returned as a public event.
    checked = [{**row, "old_price": row["new_price"] if row["old_price"] is None else row["old_price"]} for row in safe]
    integrity = inspect_price_history(checked, current_price)
    net, net_pct = None, None
    if integrity["can_compute_net_change"] and (not baseline_unknown or len(safe) > 1):
        first = safe[0]["new_price"] if baseline_unknown else safe[0]["old_price"]
        net = safe[-1]["new_price"] - first
        net_pct = round(net / first * 100, 2)
    warning = integrity["message"]
    if baseline_unknown and safe:
        warning = " ".join(filter(None, [warning, "Цена до первого зафиксированного изменения не сохранена."]))
    return {"status": integrity["status"], "events": safe, "changes_count": len(safe),
            "warning": warning, "net_change": net, "net_pct": net_pct, "baseline_unknown": baseline_unknown}


def build_offer_details(row, market, *, now):
    """Use the original listing/project address, without canonical-family guessing."""
    primary = market == "primary"
    raw = row.get("price_events", [])
    if not isinstance(raw, list):
        raise ValueError("Invalid source price events")
    events, previous = [], None
    for event in raw:
        if not isinstance(event, dict):
            raise ValueError("Invalid source price event")
        new = event.get("price") if primary else event.get("new_price")
        events.append({"at": event.get("changed_at"), "old_price": previous if primary else event.get("old_price"), "new_price": new})
        previous = new
    address = _source_address(row.get("address"))
    return {"address": address,
            "address_note": ("Адрес проекта из каталога; точный адрес корпуса уточните у застройщика." if primary
                             else "Адрес из объявления; точный номер дома и корпус подтвердите у продавца.") if address else "Точный адрес не указан в источнике.",
            "building": _text(row.get("building"), 100) if primary else None,
            "section": _text(str(row["section"]) if row.get("section") is not None else None, 100) if primary else None,
            "price_history": price_history(events, row["price"], now=now, baseline_unknown=primary)}


def validate_offer_details(row, market, current_price, *, now):
    """Public snapshot allowlist, including a nested event allowlist."""
    history = row.get("price_history")
    if not isinstance(history, dict) or history.get("baseline_unknown") is not (market == "primary"):
        raise ValueError("Invalid public price history")
    address = _text(row.get("address"), 500)
    primary = market == "primary"
    return {"address": address,
            "address_note": ("Адрес проекта из каталога; точный адрес корпуса уточните у застройщика." if primary
                             else "Адрес из объявления; точный номер дома и корпус подтвердите у продавца.") if address else "Точный адрес не указан в источнике.",
            "building": _text(row.get("building"), 100) if primary else None,
            "section": _text(row.get("section"), 100) if primary else None,
            "price_history": price_history(history.get("events"), current_price, now=now, baseline_unknown=primary)}


def details_for_selected_offer(details, displayed_price):
    """Daily browser-verified price may differ from the later scanner snapshot."""
    if not details:
        return {"address": None, "address_note": "Точный адрес пока не получен из источника.",
                "building": None, "section": None,
                "price_history": {"status": "unavailable", "events": [], "changes_count": 0,
                                  "warning": "История цены пока не загружена.", "net_change": None,
                                  "net_pct": None, "baseline_unknown": False}}
    safe = {field: details[field] for field in DETAIL_FIELDS}
    history = details["price_history"]
    # The file was validated at load time; use its newest event as the date
    # bound so a historic snapshot stays readable without changing its facts.
    now = max((_utc(event["at"]) for event in history["events"]), default=datetime.now(timezone.utc))
    safe["price_history"] = price_history(history["events"], displayed_price, now=now,
                                         baseline_unknown=history["baseline_unknown"])
    if details.get("current_price", displayed_price) != displayed_price:
        safe["price_history"].update(status="incomplete", net_change=None, net_pct=None)
        safe["price_history"]["warning"] = " ".join(filter(None, [safe["price_history"]["warning"],
            "Цена в подборке отличается от цены последнего обновления базы. Проверьте актуальную цену в источнике."]))
    return safe
