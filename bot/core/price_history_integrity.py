"""Check recorded asking-price events without inventing missing observations."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import math


def valid_price(value) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return False
    try:
        return math.isfinite(float(value)) and value > 0
    except (OverflowError, ValueError):
        return False


def _at(value):
    return value.isoformat() if isinstance(value, datetime) else value


def inspect_price_history(events: list[dict], current=None, current_at=None) -> dict:
    """Events must be in observation order; consistency does not prove completeness.

    An old/new mismatch records a gap, not a fabricated intermediate price change.
    A cached current price is compared separately with the last recorded event.
    """
    issues = []
    previous_new = None
    previous_at = None
    for index, event in enumerate(events):
        old, new = event.get("old_price"), event.get("new_price")
        at = _at(event.get("changed_at") or event.get("observed_at") or event.get("at"))
        if not valid_price(old) or not valid_price(new):
            issues.append({"code": "invalid_price_event", "event_index": index,
                           "at": at, "old_price": old, "new_price": new})
        if previous_new is not None and valid_price(old) and previous_new != old:
            issues.append({"code": "price_chain_gap", "event_index": index,
                           "at": at, "previous_at": previous_at,
                           "previous_new_price": previous_new, "next_old_price": old})
        previous_new = new if valid_price(new) else None
        previous_at = at

    latest_matches = None
    if current is not None and not valid_price(current):
        issues.append({"code": "invalid_current_price", "current_price": current,
                       "current_at": _at(current_at)})
    if events and valid_price(current) and previous_new is not None:
        latest_matches = previous_new == current
        if not latest_matches:
            issues.append({"code": "current_price_mismatch", "at": previous_at,
                           "latest_event_price": previous_new, "current_price": current,
                           "current_at": _at(current_at)})
    chain_continuous = not any(i["code"] in {"invalid_price_event", "price_chain_gap"}
                               for i in issues)
    status = "incomplete" if issues else ("consistent" if events else "no_changes_recorded")
    reasons = []
    if not chain_continuous:
        reasons.append("Между записанными изменениями цены есть пропуски или некорректные значения.")
    if latest_matches is False:
        reasons.append("Последняя цена в истории отличается от текущей цены в базе.")
    if any(i["code"] == "invalid_current_price" for i in issues):
        reasons.append("Текущая цена в базе требует проверки.")
    if reasons:
        reasons.append("Итоговое изменение и связный график не рассчитываем; отдельные события сохранены.")
    return {"status": status, "chain_continuous": chain_continuous,
            "latest_matches_current": latest_matches,
            "can_compute_net_change": bool(events) and not issues,
            "issues": issues, "message": " ".join(reasons)}
