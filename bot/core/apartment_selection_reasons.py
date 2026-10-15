"""Buyer-facing reasons derived only from recorded, structured evidence.

Asking prices are comparison evidence, not completed transaction prices.
Neither a price cut nor a seller badge proves urgency or freedom from fees.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import math
from zoneinfo import ZoneInfo

from bot.core.price_history_integrity import inspect_price_history

LOCAL_TIMEZONE = ZoneInfo("Asia/Almaty")
FACT_FIELDS = ("ceiling_height", "kitchen_area", "finish_level", "is_owner", "owner_source_verified")
FINISH_LEVELS = {"rough", "prefinish", "finished", "renovated", "furnished", "designer", "needs_repair"}


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("Timezone-aware selection evidence required")
    return value.astimezone(timezone.utc)


def selection_facts(row: dict) -> dict:
    """Sanitize source facts; descriptions, contacts and free-form AI text stay private."""
    area = _number(row.get("area"))
    ceiling = _number(row.get("ceiling_height"))
    kitchen = _number(row.get("kitchen_area"))
    return {
        "ceiling_height": round(ceiling, 2) if ceiling is not None and 2 <= ceiling <= 5 else None,
        "kitchen_area": round(kitchen, 1) if kitchen is not None and area is not None and 3 <= kitchen <= min(100, area * .6 + .1) else None,
        "finish_level": row.get("finish_level") if isinstance(row.get("finish_level"), str) and row.get("finish_level") in FINISH_LEVELS else None,
        "is_owner": row.get("is_owner") if isinstance(row.get("is_owner"), bool) else None,
        "owner_source_verified": row.get("owner_source_verified") is True,
    }


def validate_selection_facts(value, area=None) -> dict:
    """Strict optional public projection; recompute reasons rather than trusting text."""
    if value is None:
        return {**dict.fromkeys(FACT_FIELDS), "owner_source_verified": False}
    if not isinstance(value, dict):
        raise ValueError("Invalid public selection facts")
    for field in ("ceiling_height", "kitchen_area"):
        number = value.get(field)
        if number is not None and (not isinstance(number, (int, float, Decimal)) or isinstance(number, bool) or _number(number) is None):
            raise ValueError("Invalid public selection measurement")
    if value.get("finish_level") is not None and (not isinstance(value.get("finish_level"), str) or value.get("finish_level") not in FINISH_LEVELS):
        raise ValueError("Invalid public finish level")
    if value.get("is_owner") is not None and not isinstance(value.get("is_owner"), bool):
        raise ValueError("Invalid public seller type")
    if "owner_source_verified" in value and not isinstance(value["owner_source_verified"], bool):
        raise ValueError("Invalid public seller evidence")
    safe = selection_facts({**value, "area": area})
    for field in ("ceiling_height", "kitchen_area"):
        if value.get(field) is not None and safe[field] is None:
            raise ValueError("Public selection measurement outside bounds")
    return safe


def _label(value, digits=1):
    return f"{value:.{digits}f}".rstrip("0").rstrip(".").replace(".", ",")


def _money(value):
    if value >= 1_000_000:
        return f"{_label(value / 1_000_000, 2)} млн ₸"
    return f"{round(value):,} ₸".replace(",", " ")


def _recent_price_cut(offer, now):
    """Only a current, continuous chain supports a recent net reduction claim."""
    history = offer.get("price_history")
    if not isinstance(history, dict) or history.get("status") != "consistent":
        return None
    events = history.get("events")
    if not isinstance(events, list) or not events or len(events) > 5_000:
        return None
    current_price = _number(offer.get("price"))
    if current_price is None or current_price <= 0:
        return None
    checked, dates = [], []
    try:
        for index, event in enumerate(events):
            at = _utc(event.get("at"))
            if at > now or (dates and at < dates[-1]):
                return None
            old, new = event.get("old_price"), event.get("new_price")
            if old is None and index == 0 and history.get("baseline_unknown") is True:
                old = new
            checked.append({"old_price": old, "new_price": new, "at": at})
            dates.append(at)
    except (ValueError, TypeError, AttributeError):
        return None
    if not inspect_price_history(checked, offer.get("price"))["can_compute_net_change"]:
        return None
    # An intervening raise makes "price keeps falling" misleading. Report cuts
    # only when the latest actual event is itself a decrease and the window's
    # net change is also negative.
    if checked[-1]["new_price"] >= checked[-1]["old_price"]:
        return None
    for days in (30, 90):
        recent = [event for event in checked if now - timedelta(days=days) <= event["at"] <= now]
        if not recent:
            continue
        first, current = recent[0]["old_price"], recent[-1]["new_price"]
        if first <= current:
            continue
        cuts = [event for event in recent if event["new_price"] < event["old_price"]]
        if not cuts:
            continue
        # Two updates on the same date are one occasion for public copy. A
        # duplicate old/new event with a chain gap was already rejected above.
        cut_dates = {event["at"].astimezone(LOCAL_TIMEZONE).date() for event in cuts}
        if any(event["new_price"] > event["old_price"] for event in recent):
            continue
        amount, pct = first-current, (first-current)/first*100
        if not (amount >= 1_000_000 or pct >= 3 or len(cut_dates) >= 2 and pct >= 1):
            continue
        return {"days": days, "count": len(cut_dates), "amount": first - current,
                "pct": (first - current) / first * 100,
                "date": cuts[-1]["at"].astimezone(LOCAL_TIMEZONE).strftime("%d.%m.%Y")}
    return None


def strong_selection_reason(offer: dict, now=None) -> bool:
    """A middle floor alone does not justify selecting a market-priced apartment."""
    now = _utc(now or datetime.now(timezone.utc))
    facts = validate_selection_facts(offer.get("selection_facts"), offer.get("area"))
    return bool((facts["ceiling_height"] is not None and facts["ceiling_height"] >= 2.9)
                or (facts["kitchen_area"] is not None and facts["kitchen_area"] >= 12)
                or facts["finish_level"] == "furnished" or _recent_price_cut(offer, now))


def apartment_selection_reasons(offer: dict, now=None) -> list[str]:
    """One to four concise benefits; no analogue counts or unsupported superlatives."""
    now = _utc(now or datetime.now(timezone.utc))
    reasons = []
    comparison = offer.get("price_comparison")
    if isinstance(comparison, dict):
        discount = _number(comparison.get("discount_pct"))
        reference = _number(comparison.get("reference_price_per_m2"))
        area, price = _number(offer.get("area")), _number(offer.get("price"))
        place = {"в том же доме": "в этом доме", "в том же ЖК": "в этом ЖК"}.get(comparison.get("scope"), "рядом")
        if discount is not None and reference is not None and reference > 0:
            if discount >= 1:
                text = f"Цена за м² на {_label(discount)}% ниже похожих предложений {place}."
                gap = reference * area - price if area is not None and price is not None else None
                if gap is not None and gap > 0:
                    text += f" Для такой площади разница — около {_money(gap)}."
                reasons.append(text)
            elif discount <= -1:
                reasons.append(f"Цена за м² на {_label(abs(discount))}% выше похожих предложений {place}.")
            else:
                reasons.append(f"Цена за м² на уровне похожих предложений {place}.")
    # Price observations belong in the card's history chart. Keep actual cuts
    # as admission/ranking evidence in strong_selection_reason, without
    # repeating the chart as a long buyer-facing paragraph.
    facts = validate_selection_facts(offer.get("selection_facts"), offer.get("area"))
    if facts["kitchen_area"] is not None and facts["kitchen_area"] >= 12:
        reasons.append(f"Кухня {_label(facts['kitchen_area'])} м² — размер указан в объявлении.")
    if facts["ceiling_height"] is not None and facts["ceiling_height"] >= 2.9:
        reasons.append(f"Высокие потолки {_label(facts['ceiling_height'], 2)} м — по данным объявления.")
    # Repair categories are seller text, not evidence of workmanship quality.
    # A poor or unassessed repair must not become an advantage or justify
    # selection. Keep independently recorded furniture as a separate fact.
    if facts["finish_level"] == "furnished":
        reasons.append("В объявлении указана мебель — часть обустройства уже есть.")
    return reasons[:4]


def best_apartment_factors(offer: dict, now=None) -> list[dict]:
    """Independent positive evidence for the curated page, strongest first.

    Market parity, a premium, generic repair labels and missing information
    never fill one of the three positions. An owner label is a source claim,
    not proof of title or freedom from intermediary fees.
    """
    now = _utc(now or datetime.now(timezone.utc))
    factors = []
    comparison = offer.get("price_comparison")
    if isinstance(comparison, dict) and (_number(comparison.get("discount_pct")) or 0) >= 1:
        price_reason = next((text for text in apartment_selection_reasons(offer, now)
                             if text.startswith("Цена за м²") and " ниже " in text), None)
        if price_reason:
            factors.append({"key": "price", "text": price_reason})
    cut = _recent_price_cut(offer, now)
    if cut:
        if cut["count"] >= 2:
            text = f"{cut['count']} снижения цены на разных датах за {cut['days']} дней: −{_money(cut['amount'])} ({_label(cut['pct'])}%)."
        else:
            text = f"Цена снижена на {_money(cut['amount'])} ({_label(cut['pct'])}%) за {cut['days']} дней."
        factors.append({"key": "price_history", "text": text})
    facts = validate_selection_facts(offer.get("selection_facts"), offer.get("area"))
    if facts["ceiling_height"] is not None and facts["ceiling_height"] >= 2.9:
        factors.append({"key": "ceiling", "text": f"Высокие потолки {_label(facts['ceiling_height'], 2)} м — по данным объявления."})
    if facts["kitchen_area"] is not None and facts["kitchen_area"] >= 12:
        factors.append({"key": "kitchen", "text": f"Кухня {_label(facts['kitchen_area'])} м² — размер указан в объявлении."})
    if facts["is_owner"] is True and facts["owner_source_verified"] is True:
        factors.append({"key": "owner", "text": "В источнике продавец указан как хозяин."})
    floor, floors = _number(offer.get("floor")), _number(offer.get("floors_total"))
    if floor is not None and floors is not None and floor.is_integer() and floors.is_integer() and 1 < floor < floors <= 60:
        factors.append({"key": "floor", "text": f"{int(floor)}-й этаж из {int(floors)} — между первым и последним."})
    return factors


def best_apartment_reasons(offer: dict, now=None) -> list[str]:
    return [factor["text"] for factor in best_apartment_factors(offer, now)[:3]]
