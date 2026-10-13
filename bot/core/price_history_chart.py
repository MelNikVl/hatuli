"""SVG coordinates for recorded asking-price events, without invented dates.

Both ends of a change share its recorded timestamp. A connector is drawn only
when consecutive events agree on price, and is styled as an observation gap by
the template. No first price is extended into an unobserved past.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
from zoneinfo import ZoneInfo


WIDTH, HEIGHT = 360, 184
LEFT, RIGHT, TOP, BOTTOM = 64, 344, 22, 137
LOCAL_TIMEZONE = ZoneInfo("Asia/Qyzylorda")


def _price(value):
    if isinstance(value, bool):
        raise ValueError("Invalid chart price")
    number = float(value)
    if not math.isfinite(number) or not number.is_integer() or not 0 < number <= 10_000_000_000:
        raise ValueError("Invalid chart price")
    return int(number)


def _date(value):
    at = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if not isinstance(at, datetime) or at.tzinfo is None:
        raise ValueError("Invalid chart date")
    return at.astimezone(timezone.utc)


def _million(value, span):
    if value < 1_000_000:
        return f"{value:,} ₸".replace(",", " ")
    # Very small real edits must not acquire identical rounded axis labels.
    digits = min(6, max(2, 6-int(math.log10(span)))) if span else 2
    number = f"{value / 1_000_000:.{digits}f}".rstrip("0").rstrip(".").replace(".", ",")
    return f"{number} млн"


def price_history_chart(history):
    """Return safe chart geometry, or None for no events / invalid input.

The input is the public projection from comparison_offer_details.price_history.
Do not copy arbitrary text, stored SVG, or asserted point coordinates into HTML.
"""
    if not isinstance(history, dict) or not isinstance(history.get("events"), list):
        return None
    events = history["events"]
    if not events or len(events) > 5_000:
        return None
    safe = []
    try:
        previous_at = None
        for index, event in enumerate(events):
            at = _date(event["at"])
            if previous_at is not None and at < previous_at:
                raise ValueError("Unordered chart dates")
            old = None if event.get("old_price") is None else _price(event["old_price"])
            if old is None and index != 0:
                raise ValueError("Unknown later chart price")
            new = _price(event["new_price"])
            safe.append({"at": at.isoformat(), "date_label": at.astimezone(LOCAL_TIMEZONE).strftime("%d.%m.%Y"),
                         "stamp": at.timestamp(), "old_price": old, "new_price": new,
                         "delta": new-old if old is not None else None,
                         "direction": "down" if old is not None and new < old else "up" if old is not None and new > old else "unknown"})
            previous_at = at
    except (TypeError, ValueError, KeyError, OverflowError):
        return None

    prices = [value for event in safe for value in (event["old_price"], event["new_price"]) if value is not None]
    low, high = min(prices), max(prices)
    padding = max((high-low)*.09, high*.004, 10_000)
    floor, ceiling = low-padding, high+padding
    span = safe[-1]["stamp"]-safe[0]["stamp"]

    def x(event):
        return round(LEFT+(event["stamp"]-safe[0]["stamp"])/span*(RIGHT-LEFT), 2) if span else round((LEFT+RIGHT)/2, 2)

    def y(price):
        return round(BOTTOM-(price-floor)/(ceiling-floor)*(BOTTOM-TOP), 2)

    connectors, changes, points = [], [], []
    for index, event in enumerate(safe):
        position = x(event)
        if index and event["old_price"] is not None and safe[index-1]["new_price"] == event["old_price"]:
            connectors.append({"x1": x(safe[index-1]), "x2": position,
                               "y": y(event["old_price"])})
        if event["old_price"] is not None:
            changes.append({"x": position, "y1": y(event["old_price"]), "y2": y(event["new_price"]),
                            "direction": event["direction"], "old_price": event["old_price"],
                            "new_price": event["new_price"], "date_label": event["date_label"]})
            points.append({"x": position, "y": y(event["old_price"]), "price": event["old_price"],
                           "date_label": event["date_label"], "kind": "before", "direction": event["direction"]})
        points.append({"x": position, "y": y(event["new_price"]), "price": event["new_price"],
                       "date_label": event["date_label"], "kind": "after", "direction": event["direction"]})

    has_gap = any(safe[index-1]["new_price"] != event["old_price"] for index, event in enumerate(safe) if index)
    first = safe[0]["old_price"] if safe[0]["old_price"] is not None else safe[0]["new_price"]
    net = safe[-1]["new_price"]-first if history.get("status") == "consistent" and not has_gap and (safe[0]["old_price"] is not None or len(safe)>1) else None
    ticks = [{"x": x(safe[0]), "label": safe[0]["date_label"], "anchor": "start" if span else "middle"}]
    if span:
        ticks.append({"x": x(safe[-1]), "label": safe[-1]["date_label"], "anchor": "end"})
    # Only label a middle event when its actual timestamp leaves enough space.
    if len(safe) > 2 and span:
        middle = min(safe[1:-1], key=lambda event: abs(x(event)-(LEFT+RIGHT)/2))
        if LEFT+90 < x(middle) < RIGHT-90:
            ticks.insert(1, {"x": x(middle), "label": middle["date_label"], "anchor": "middle"})
    axis = [{"y": y(high), "label": _million(high, high-low)}, {"y": y(low), "label": _million(low, high-low)}] if high != low else [{"y": y(high), "label": _million(high, 0)}]
    return {"width": WIDTH, "height": HEIGHT, "axis": axis, "date_ticks": ticks,
            "connectors": connectors, "changes": changes, "points": points,
            "net_change": net, "net_pct": round(net/first*100, 2) if net is not None else None,
            "has_gap": has_gap, "events_count": len(safe),
            "summary": f"История цены предложения: {len(safe)} наблюдений, с {safe[0]['date_label']} по {safe[-1]['date_label']}."}
