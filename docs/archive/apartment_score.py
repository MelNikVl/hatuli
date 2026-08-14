"""
Apartment investment scoring (0-100).

YIELD(20) + PRICE_VS_MARKET(15) + LOCATION_DEMAND(20) +
APT_TYPE(15) + BUILDING(10) + SUPPLY(10) + CAPITAL_GROWTH(10)
"""
import re


def _yield_score(yield_pct):
    if yield_pct >= 13: return 20, f"Yield {yield_pct}% — отличный"
    if yield_pct >= 10: return 15, f"Yield {yield_pct}% — хороший"
    if yield_pct >= 8: return 10, f"Yield {yield_pct}% — нормальный"
    if yield_pct >= 5: return 5, f"Yield {yield_pct}% — слабый"
    return 2, f"Yield {yield_pct}% — низкий"


def _price_vs_market(price_per_m2, district_avg_m2):
    if not district_avg_m2 or district_avg_m2 == 0:
        return 5, "нет данных по району"
    diff = (price_per_m2 - district_avg_m2) / district_avg_m2
    if diff < -0.15: return 15, f"на {abs(diff)*100:.0f}% ниже рынка"
    if diff < -0.05: return 10, f"на {abs(diff)*100:.0f}% ниже рынка"
    if diff < 0.05: return 5, "на уровне рынка"
    return 2, f"на {diff*100:.0f}% выше рынка"


_LOC_SCORES = {
    "есиль": (20, "деловой центр, высокий спрос"),
    "алматы": (16, "развитая инфраструктура"),
    "сарыарка": (12, "спальный, стабильный спрос"),
    "байконур": (10, "спальный, растущий"),
    "нура": (8, "окраина, низкий спрос"),
}

_POI_KW = ["трц", "тц", "бц", "университет", "expo", "хан шатыр",
           "мега", "назарбаев", "больниц", "госпитал", "школ", "метро", "lrt"]


def _location_score(district, address, title):
    text = f"{district} {address} {title}".lower()
    score, reason = 5, "район не определён"
    for key, (val, desc) in _LOC_SCORES.items():
        if key in text:
            score, reason = val, desc
            break
    poi = [kw for kw in _POI_KW if kw in text]
    if poi and score < 20:
        bonus = min(3, 20 - score)
        score += bonus
        reason += f" + {poi[0]}"
    return score, reason


def _apt_type_score(rooms, area):
    reasons = []
    score = 8
    if rooms == 1:
        score = 15
        reasons.append("1-комн — макс спрос")
    elif rooms == 2:
        score = 13
        reasons.append("2-комн — семьи, стабильно")
    elif rooms == 3:
        score = 8
        reasons.append("3-комн — долго ищет арендатора")
    elif rooms and rooms >= 4:
        score = 5
        reasons.append("4+ комн — узкий спрос")

    if area:
        if 35 <= area <= 55:
            reasons.append(f"{area}м² оптимум")
        elif area > 100:
            score = max(score - 3, 2)
            reasons.append(f"{area}м² большая")
    return score, ", ".join(reasons) if reasons else "тип не определён"


def _building_score(title, description):
    text = f"{title} {description}".lower()
    score = 4
    reason = "возраст не определён"
    year_match = re.search(r"20(2[4-9]|3[0-9])", text)
    if year_match:
        return 10, f"новостройка {year_match.group()}"
    year_match2 = re.search(r"20(1[0-9]|2[0-3])", text)
    if year_match2:
        return 7, f"дом {year_match2.group()}"
    if "новостройк" in text or "новый дом" in text or "сдан" in text:
        return 9, "новостройка"
    return score, reason


def _supply_score(same_complex_count):
    if same_complex_count <= 3:
        return 10, f"дефицит ({same_complex_count} в ЖК)"
    if same_complex_count <= 8:
        return 6, f"умеренно ({same_complex_count} в ЖК)"
    return 3, f"много предложений ({same_complex_count} в ЖК)"


def _capital_growth_score():
    # TODO: parse historical prices. For now use Astana avg +8.5%
    return 7, "рынок растёт ~8.5%/год"


def compute_apartment_score(listing, same_complex_count=1, district_avg_m2=None):
    """Compute apartment investment score 0-100."""
    yield_pct = listing.get("yield_pct", 0)
    district = listing.get("district", "")
    address = listing.get("address", "")
    title = listing.get("title", "")
    rooms = listing.get("rooms")
    area = listing.get("area")
    price = listing.get("price", 0)
    description = listing.get("description", "")
    price_m2 = price / area if (area and area > 0) else 0

    s1, r1 = _yield_score(yield_pct)
    s2, r2 = _price_vs_market(price_m2, district_avg_m2)
    s3, r3 = _location_score(district, address, title)
    s4, r4 = _apt_type_score(rooms, area)
    s5, r5 = _building_score(title, description)
    s6, r6 = _supply_score(same_complex_count)
    s7, r7 = _capital_growth_score()

    total = s1 + s2 + s3 + s4 + s5 + s6 + s7

    return {
        "total_score": total,
        "breakdown": {
            "yield": s1, "price_market": s2, "location": s3,
            "apt_type": s4, "building": s5, "supply": s6, "growth": s7,
        },
        "reasons": [r1, r2, r3, r4, r5, r6, r7],
    }


APT_MIN_ALERT_SCORE = 65
APT_MAX_ALERTS = 5
