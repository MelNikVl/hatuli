"""One-budget comparison from already published, qualified observations.

This module neither fetches sources nor opens a database. A rental asking
range is not a forecast of rent received, occupancy, expenses or resale value.
Unknown operating inputs therefore never become zero-cost net returns.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone

from bot.core.comparison_rental_estimate import validate_rental_estimate
from bot.core.daily_pick_identity import validate_public_daily_identity
from bot.core.daily_picks import sanitized_listing_address, snapshot_status
from bot.core.investment_strategies import calculate_strategy_comparison
from bot.core.sale_source_quarantine import QUARANTINED_SOURCE_IDS

LOCAL_TIMEZONE = timezone(timedelta(hours=5))
MAX_PUBLICATION_AGE = timedelta(hours=26)
MAX_OBSERVATION_AGE = timedelta(hours=72)
MAX_CATALOG_AGE = timedelta(days=7)
MAX_RESULTS = 6


def _utc(value):
    try:
        date = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return date.astimezone(timezone.utc) if date.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def _fresh(value, now, age):
    stamp = _utc(value)
    return stamp is not None and now-age <= stamp <= now


def _number(value, low, high):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return number if math.isfinite(number) and low <= number <= high else None


def automatic_budget(value):
    """Parse a money input, including the usual thousands separators."""
    if isinstance(value, str):
        if len(value) > 80:
            raise ValueError("Общий бюджет: требуется число")
        value = value.strip().replace(" ", "").replace("\u00a0", "").replace("\u202f", "")
    number = _number(value, 1, 1_000_000_000)
    if number is None:
        raise ValueError("Общий бюджет: допустима сумма от 1 до 1 000 000 000 ₸")
    return round(number, 2)


def select_complex(value, catalog, now):
    """Only the separately published city catalogue authorizes a filter ID."""
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).isascii() or not str(value).isdecimal():
        raise ValueError("Выберите ЖК из каталога Астаны")
    identifier = int(value)
    if not 1 <= identifier <= 2_147_483_647:
        raise ValueError("Выберите ЖК из каталога Астаны")
    if not _catalog_current(catalog, now):
        raise ValueError("Выбранный ЖК отсутствует в актуальном каталоге Астаны")
    row = next((row for row in catalog["items"] if row.get("id") == identifier), None)
    if row is None:
        raise ValueError("Выбранный ЖК отсутствует в актуальном каталоге Астаны")
    return row


def _catalog_current(catalog, now):
    return (isinstance(catalog, dict) and catalog.get("scope") == "astana"
            and isinstance(catalog.get("items"), list)
            and _fresh(catalog.get("generated_at"), now, MAX_CATALOG_AGE))


def _unchanged(current, item):
    if not isinstance(current, dict):
        return True
    if current.get("current_price", current.get("price")) != item["price"]:
        return False
    for field in ("rooms", "area", "floor", "floors_total"):
        if current.get(field) is not None and current[field] != item.get(field):
            return False
    return True


def _observed_purchases(market, picks, complexes, selected, budget, now):
    """Positive source identity is required, never inferred from a catalogue FK.

    The current market schema lacks identity receipts. Daily picks are the
    qualified positive subset; a matching current market offer may supplement
    their rental range, but cannot silently change the checked identity.
    """
    if not _catalog_current(complexes, now) or not isinstance(picks, dict) or not isinstance(market, dict):
        return []
    if not _fresh(market.get("generated_at"), now, MAX_PUBLICATION_AGE):
        return []
    try:
        if snapshot_status(picks, now)["is_stale"] or not _fresh(picks.get("generated_at"), now, MAX_PUBLICATION_AGE):
            return []
    except (KeyError, ValueError, TypeError):
        return []
    catalogue = {row["id"]: row for row in complexes["items"]}
    group = ({selected["id"], *selected.get("child_ids", [])} if selected and selected.get("is_umbrella") is True
             else {selected["id"]} if selected else None)
    offers = {row.get("id"): row for row in market.get("offers", []) if row.get("market_type") == "secondary"}
    details = {row.get("id"): row for row in market.get("listing_details", [])}
    rows, signatures, ids = [], set(), set()
    for item in picks.get("items", []):
        if not isinstance(item, dict):
            continue
        identifier = item.get("id")
        if (not isinstance(identifier, str) or not re.fullmatch(r"[0-9]{5,20}", identifier)
                or identifier in ids or identifier in QUARANTINED_SOURCE_IDS
                or item.get("source_status") != "active"
                or not _fresh(item.get("source_checked_at"), now, MAX_PUBLICATION_AGE)
                or not _fresh(item.get("observed_at"), now, MAX_OBSERVATION_AGE)):
            continue
        try:
            identity = validate_public_daily_identity(item.get("complex_identity"))
        except (ValueError, TypeError):
            continue
        cid = identity["canonical_id"]
        canonical = catalogue.get(cid)
        if identity["status"] not in {"catalog_bound", "house_from_source"} or canonical is None or canonical.get("is_umbrella") is True:
            continue
        if group is not None and cid not in group:
            continue
        if identity["umbrella_id"] is not None and canonical.get("parent_id") != identity["umbrella_id"]:
            continue
        price = _number(item.get("price"), 1, budget)
        area = _number(item.get("area"), 20, 300)
        rooms = _number(item.get("rooms"), 1, 6)
        url = item.get("source_url")
        if (price is None or area is None or rooms is None or not rooms.is_integer()
                or url not in {f"https://krisha.kz/a/show/{identifier}", f"https://krisha.kz/a/show/{identifier}/"}):
            continue
        current = offers.get(identifier)
        supplemental = details.get(identifier)
        if not _unchanged(current, item) or not _unchanged(supplemental, item):
            continue
        if current is not None and (current.get("complex_id") != cid or not _fresh(current.get("observed_at"), now, MAX_OBSERVATION_AGE)):
            continue
        address, _note = sanitized_listing_address(item.get("address"), price, now)
        if not address:
            continue
        signature = (cid, address.casefold(), rooms, round(area, 1), price, item.get("floor"))
        if signature in signatures:
            continue
        signatures.add(signature)
        ids.add(identifier)
        checked = _utc(item["source_checked_at"])
        row = {"id": identifier, "title": f"{int(rooms)}-комнатная · {area:g} м²",
               "complex_id": cid, "complex_name": identity["canonical_name"], "address": address,
               "rooms": int(rooms), "area": area, "price": price,
               "cash_remainder": round(budget-price, 2),
               "href": f"/compare?listing={identifier}#investment", "url": url,
               "observed_at_label": checked.astimezone(LOCAL_TIMEZONE).strftime("%d.%m.%Y, %H:%M"),
               "rental_estimate": None}
        # Supplemental detail projections do not carry their complex ID. Only
        # the current full offer can prove this estimate uses the same project.
        if current is not None:
            row["rental_estimate"] = validate_rental_estimate(current.get("rental_estimate"), now)
        rows.append(row)
    return rows


def calculate_automatic_investment(payload, *, catalog, market, picks, complexes, now=None):
    """Only budget and optional complex_id influence the automatic calculation.

    Manual scenario fields are ignored. We cannot infer achieved daily ADR,
    occupancy, construction exit prices, expenses or net property returns.
    """
    if not isinstance(payload, dict):
        raise ValueError("Требуется объект параметров")
    now = _utc(now or datetime.now(timezone.utc))
    if now is None:
        raise ValueError("Требуется дата с часовым поясом")
    budget = automatic_budget(payload.get("budget"))
    selected = select_complex(payload.get("complex_id"), complexes, now)
    bank_result = calculate_strategy_comparison({"budget": budget}, catalog=catalog, now=now)
    banks = sorted(bank_result["banks"], key=lambda row: (-row["total_profit"], row["bank_key"]))
    best = banks[0] if banks else None
    candidates = _observed_purchases(market, picks, complexes, selected, budget, now)
    properties, purchases = [], []
    for candidate in candidates:
        row = dict(candidate)
        estimate = row["rental_estimate"]
        if estimate is None:
            del row["rental_estimate"]
            purchases.append(row)
            continue
        annual = estimate["monthly_median"]*12
        row.update(annual_gross_ceiling=annual,
                   gross_yield_on_budget_pct=round(annual/budget*100, 2), bank_threshold=None)
        if best:
            income = best["total_profit"]
            row["bank_threshold"] = {"bank": best["bank"], "product": best["product"], "annual_income": income,
                                     "minimum_monthly_net_cash": (math.floor(income/12*100)+1)/100,
                                     "annual_gross_difference": round(annual-income, 2), "is_upper_bound": True}
        properties.append(row)
    properties.sort(key=lambda row: (-row["annual_gross_ceiling"], row["price"], row["id"]))
    purchases.sort(key=lambda row: (-row["price"], row["id"]))
    return {"schema_version": 1, "mode": "automatic", "budget": budget, "initial_capital": budget,
            "horizon_months": 12, "complex_id": selected["id"] if selected else None,
            "location_label": selected["label"] if selected else "Астана",
            "banks": banks, "best_bank": best, "best_bank_key": best["key"] if best else None,
            "properties": properties[:MAX_RESULTS], "purchases": purchases[:MAX_RESULTS], "strategies": [],
            "guarantee_source": bank_result["guarantee_source"],
            "market_date_label": (_utc(market.get("generated_at")).astimezone(LOCAL_TIMEZONE).strftime("%d.%m.%Y, %H:%M")
                                  if isinstance(market, dict) and _utc(market.get("generated_at")) else None),
            "scope": "Проверенные опубликованные предложения Hatuli; не весь рынок Астаны",
            "empty_message": ("Для этой суммы и выбранного ЖК сейчас нет подходящих проверенных вариантов."
                              if selected and not banks and not properties and not purchases else
                              "Для этой суммы сейчас нет подходящих проверенных вариантов."
                              if not banks and not properties and not purchases else None),
            "diagnostics": [],
            "assumptions": ["Один бюджет на 12 месяцев без ипотеки.",
                            "Остаток после цены покупки указан до расходов оформления и ремонта; проценты на него не начислены.",
                            "Годовая валовая аренда — ставка предложения за 12 месяцев без простоя и до расходов, а не чистый доход.",
                            "Будущая цена квартиры, срок продажи и фактическая загрузка не прогнозируются."]}
