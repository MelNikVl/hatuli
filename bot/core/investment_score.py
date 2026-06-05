"""
Investment scoring for storage units & parking spots.

Score 0-100:
  YIELD (25) + LOCATION (25) + SUPPLY_DEMAND (20) + LIQUIDITY (15) + QUALITY (15)
"""
from __future__ import annotations

import re
from typing import Any


# ── 1. YIELD SCORE (max 25) ──────────────────────────────────────────────────

def _yield_score(yield_pct: float) -> tuple[int, str]:
    if yield_pct >= 20:
        return 25, f"Yield {yield_pct}% — отличный"
    if yield_pct >= 15:
        return 20, f"Yield {yield_pct}% — хороший"
    if yield_pct >= 12:
        return 15, f"Yield {yield_pct}% — приемлемый"
    return 5, f"Yield {yield_pct}% — низкий"


# ── 2. LOCATION SCORE (max 25) ──────────────────────────────────────────────

_LOCATION_SCORES: dict[str, int] = {
    "есиль": 25,
    "есильский": 25,
    "expo": 25,
    "экспо": 25,
    "левый берег": 25,
    "алматы р-н": 18,
    "алматинский": 18,
    "сарыарка": 14,
    "сарыаркинский": 14,
    "нура": 10,
}

_POI_KEYWORDS = ["трц", "тц", "молл", "mall", "бц", "бизнес-центр",
                 "университет", "универ", "назарбаев", "expo", "хан шатыр",
                 "мега", "керуен"]


def _location_score(district: str, address: str, title: str) -> tuple[int, str]:
    text = f"{district} {address} {title}".lower()
    score = 0
    reason = "район не определён"

    for key, val in _LOCATION_SCORES.items():
        if key in text:
            score = val
            reason = f"район: {key} ({val}б)"
            break

    # POI bonus (max +5, capped at 25 total)
    poi_found = [kw for kw in _POI_KEYWORDS if kw in text]
    if poi_found and score < 25:
        bonus = min(5, 25 - score)
        score += bonus
        reason += f" + рядом {poi_found[0]}"

    return score, reason


# ── 3. SUPPLY-DEMAND SCORE (max 20) ─────────────────────────────────────────

def _supply_demand_score(same_complex_count: int) -> tuple[int, str]:
    if same_complex_count <= 2:
        return 20, f"дефицит ({same_complex_count} в ЖК)"
    if same_complex_count <= 5:
        return 12, f"умеренно ({same_complex_count} в ЖК)"
    return 5, f"переизбыток ({same_complex_count} в ЖК)"


# ── 4. LIQUIDITY SCORE (max 15) ─────────────────────────────────────────────

def _liquidity_score(area: float | None, price: int, prop_type: str) -> tuple[int, str]:
    score = 0
    reasons = []

    if area:
        if prop_type == "garage":
            if 13 <= area <= 20:
                score += 8
                reasons.append("площадь стандартная")
            elif area < 10:
                score += 3
                reasons.append("площадь маловата")
            else:
                score += 5
                reasons.append("площадь большая")
        else:  # storage / commercial
            if 6 <= area <= 15:
                score += 8
                reasons.append("площадь оптимальная")
            elif area < 4:
                score += 2
                reasons.append("слишком мелкая")
            elif area > 25:
                score += 4
                reasons.append("большая площадь")
            else:
                score += 6
                reasons.append("площадь нормальная")
    else:
        score += 4
        reasons.append("площадь не указана")

    if price <= 4_000_000:
        score += 7
        reasons.append("цена ≤4М — ликвидно")
    elif price <= 7_000_000:
        score += 4
        reasons.append("цена средняя")
    else:
        score += 1
        reasons.append("дорого — дольше окупать")

    return min(score, 15), ", ".join(reasons)


# ── 5. QUALITY SIGNALS (max 15) ─────────────────────────────────────────────

_CLIMATE_KW = ["отапливаем", "тёпл", "тепл", "+15", "+10", "теплый",
               "обогрев", "climate"]
_SECURITY_KW = ["охран", "24/7", "видеонаблюд", "камер", "пост охран",
                "шлагбаум", "домофон"]


def _quality_score(title: str, address: str, published_at: str, description: str = "") -> tuple[int, str]:
    text = f"{title} {address} {description}".lower()
    score = 0
    reasons = []

    if any(kw in text for kw in _CLIMATE_KW):
        score += 6
        reasons.append("отапливается")

    if any(kw in text for kw in _SECURITY_KW):
        score += 4
        reasons.append("охрана/безопасность")

    # Новый ЖК — пытаемся найти год в тексте
    year_match = re.search(r"20(2[0-9]|3[0-9])", text)
    if year_match:
        year = int(year_match.group())
        if year >= 2020:
            score += 5
            reasons.append(f"новый ЖК ({year})")

    return min(score, 15), ", ".join(reasons) if reasons else "нет доп. сигналов"


# ── TOTAL SCORE ─────────────────────────────────────────────────────────────

def compute_score(listing: dict[str, Any], same_complex_count: int = 1) -> dict[str, Any]:
    """
    Compute investment score (0-100) for a listing.

    Returns dict with: total_score, breakdown, reasons.
    """
    yield_pct = listing.get("yield_pct", 0)
    district = listing.get("district", "")
    address = listing.get("address", "")
    title = listing.get("title", "")
    area = listing.get("area")
    price = listing.get("price", 0)
    prop_type = listing.get("prop_type", "commercial")
    published_at = listing.get("published_at", "")

    s1, r1 = _yield_score(yield_pct)
    s2, r2 = _location_score(district, address, title)
    s3, r3 = _supply_demand_score(same_complex_count)
    s4, r4 = _liquidity_score(area, price, prop_type)
    description = listing.get("description", "")
    s5, r5 = _quality_score(title, address, published_at, description)

    total = s1 + s2 + s3 + s4 + s5

    return {
        "total_score": total,
        "breakdown": {
            "yield": s1,
            "location": s2,
            "supply_demand": s3,
            "liquidity": s4,
            "quality": s5,
        },
        "reasons": [r1, r2, r3, r4, r5],
    }


MIN_ALERT_SCORE = 65
MAX_ALERTS_PER_CYCLE = 5
