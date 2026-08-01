"""
Relevance scoring module.

score(listing, prefs) -> (int, List[str])
  Returns an integer score and a list of Russian-language reason strings
  explaining the score breakdown.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _parse_date(date_str: str | None) -> datetime | None:
    """Try to parse an ISO datetime string; return None on failure."""
    if not date_str:
        return None
    # Python 3.11+ fromisoformat handles all ISO 8601 variants including +00:00
    try:
        dt = datetime.fromisoformat(date_str)
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except (ValueError, TypeError):
        pass
    # Fallback for truncated strings
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(date_str[:len(fmt)], fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _budget_score(price: int | None, prefs: dict[str, Any]) -> tuple[int, list[str]]:
    reasons: list[str] = []
    if price is None:
        return 0, reasons

    budget_min: int | None = prefs.get("budget_min")
    budget_max: int | None = prefs.get("budget_max")

    if budget_max is None:
        return 0, reasons

    midpoint = budget_max if budget_min is None else (budget_min + budget_max) / 2

    delta = price - midpoint
    pct = abs(delta) / midpoint if midpoint else 0

    if price <= budget_max and (budget_min is None or price >= budget_min):
        if pct <= 0.05:
            reasons.append("Цена точно в бюджете")
            return 20, reasons
        reasons.append("Цена укладывается в бюджет")
        return 10, reasons

    # Over budget
    if pct <= 0.15:
        reasons.append("Цена незначительно превышает бюджет (до 15%)")
        return -10, reasons
    if pct <= 0.30:
        reasons.append("Цена заметно выше бюджета (15–30%)")
        return -25, reasons

    reasons.append(f"Цена значительно выше бюджета (+{int(pct * 100)}%)")
    return -40, reasons


def _district_score(listing: dict[str, Any], prefs: dict[str, Any]) -> tuple[int, list[str]]:
    reasons: list[str] = []
    preferred_district: str | None = prefs.get("district")
    listing_district: str | None = listing.get("district") or ""

    if not preferred_district:
        return 0, reasons

    preferred_lower = preferred_district.lower().strip()
    listing_lower = listing_district.lower().strip()

    if preferred_lower and listing_lower and preferred_lower in listing_lower:
        reasons.append(f"Район совпадает: {listing_district}")
        return 15, reasons

    if listing_lower:
        reasons.append(f"Район не совпадает с предпочтительным ({preferred_district})")
        return -10, reasons

    return 0, reasons


def _rooms_score(listing: dict[str, Any], prefs: dict[str, Any]) -> tuple[int, list[str]]:
    reasons: list[str] = []
    pref_rooms: list | None = prefs.get("rooms")  # e.g. ["1", "2"] or [2, 3]
    listing_rooms: int | None = listing.get("rooms")

    if not pref_rooms or listing_rooms is None:
        return 0, reasons

    # Normalise preference to a set of ints (4+ → any value >= 4)
    pref_set: set[int] = set()
    has_4plus = False
    for r in pref_rooms:
        r_str = str(r).strip()
        if r_str in ("4+", "4 и более"):
            has_4plus = True
        else:
            try:
                pref_set.add(int(r_str))
            except ValueError:
                pass

    matches = listing_rooms in pref_set or (has_4plus and listing_rooms >= 4)

    if matches:
        reasons.append(f"Количество комнат совпадает: {listing_rooms}")
        return 15, reasons

    reasons.append(f"Количество комнат не подходит (нужно {'/'.join(str(r) for r in sorted(pref_set))}{'и 4+' if has_4plus else ''}, предлагается {listing_rooms})")
    return -30, reasons


def _area_score(listing: dict[str, Any], prefs: dict[str, Any]) -> tuple[int, list[str]]:
    reasons: list[str] = []
    area: float | None = listing.get("area")
    area_min: float | None = prefs.get("area_min")

    if area is None or area_min is None:
        return 0, reasons

    if area >= area_min:
        dev = (area - area_min) / area_min if area_min else 0
        if dev <= 0.10:
            reasons.append(f"Площадь {area:.0f} м² соответствует требованиям")
            return 10, reasons
        reasons.append(f"Площадь {area:.0f} м² — с запасом (>{area_min:.0f} м²)")
        return 10, reasons

    # Below minimum
    dev = (area_min - area) / area_min if area_min else 0
    penalty = int(dev / 0.10) * 5
    reasons.append(f"Площадь {area:.0f} м² меньше желаемых {area_min:.0f} м² (-{penalty} баллов)")
    return -penalty, reasons


def _suspicious_score(listing: dict[str, Any]) -> tuple[int, list[str]]:
    reasons: list[str] = []
    score = 0

    # No photos
    if not listing.get("photo_url"):
        score -= 15
        reasons.append("Нет фотографий — стоит проверить объявление")

    # Price suspiciously low relative to area
    price: int | None = listing.get("price")
    area: float | None = listing.get("area")
    if price and area and area > 0:
        price_m2 = price / area
        # Very rough thresholds; for rent in KZT these are ballpark
        if price_m2 < 500:  # < 500 ₸/м² per month
            score -= 25
            reasons.append("Цена подозрительно низкая — возможно мошенничество")

    return score, reasons


def _age_score(listing: dict[str, Any]) -> tuple[int, list[str]]:
    reasons: list[str] = []
    published = _parse_date(listing.get("published_at") or listing.get("found_at"))
    if not published:
        return 0, reasons

    now = datetime.now(timezone.utc)
    age_days = (now - published).days

    if age_days > 30:
        reasons.append(f"Объявление опубликовано {age_days} дней назад")
        return -5, reasons

    return 0, reasons


def _priorities_score(listing: dict[str, Any], prefs: dict[str, Any]) -> tuple[int, list[str]]:
    """Score based on user priorities like metro proximity, school, renovation, owner."""
    reasons: list[str] = []
    priorities: list[str] = prefs.get("priorities") or []
    if not priorities:
        return 0, reasons

    score = 0
    title = (listing.get("title") or "").lower()
    address = (listing.get("address") or "").lower()
    combined = f"{title} {address}"

    if "school" in priorities:
        if "школ" in combined:
            score += 5
            reasons.append("Рядом со школой")

    if "no_renovation" in priorities or "без ремонта" in priorities:
        if "без ремонта" in combined or "евроремонт" not in combined:
            score += 5
            reasons.append("Без необходимости ремонта")

    if "owner" in priorities or "от собственника" in priorities:
        if "от собственника" in combined or "хозяин" in combined:
            score += 10
            reasons.append("От собственника напрямую")

    return score, reasons


def score(listing: dict[str, Any], prefs: dict[str, Any]) -> tuple[int, list[str]]:
    """
    Compute relevance score for a listing against user preferences.

    Args:
        listing: Listing dict with fields: price, area, rooms, district,
                 photo_url, phone, published_at, found_at, title, address.
        prefs:   User prefs dict with fields: budget_min, budget_max, district,
                 rooms (list), area_min, priorities (list), move_in.

    Returns:
        (total_score, reasons) where reasons is a list of Russian strings.
    """
    total = 0
    all_reasons: list[str] = []

    for fn in (
        lambda: _budget_score(listing.get("price"), prefs),
        lambda: _district_score(listing, prefs),
        lambda: _rooms_score(listing, prefs),
        lambda: _area_score(listing, prefs),
        lambda: _suspicious_score(listing),
        lambda: _age_score(listing),
        lambda: _priorities_score(listing, prefs),
    ):
        try:
            pts, reasons = fn()
            total += pts
            all_reasons.extend(reasons)
        except Exception:  # noqa: BLE001
            pass  # never crash on bad listing data

    return total, all_reasons


def top_positive_reasons(reasons: list[str], n: int = 3) -> list[str]:
    """
    Return up to n reasons that are positive/neutral (don't start with
    typical negative markers).
    """
    negative_markers = ("не подход", "выше бюд", "нет фото", "подозри", "меньше", "не совпад", "значительно", "заметно выше")
    positives = [r for r in reasons if not any(r.lower().startswith(m) for m in negative_markers)]
    return positives[:n]
