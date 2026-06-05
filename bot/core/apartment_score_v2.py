"""
Переработанный скоринг квартир v2.

Изменения vs v1:
  - Yield считается из rental_index (реальные данные), не хардкод
  - Этаж: штраф за 1й и последний
  - Метраж: штраф ликвидности за 70м+
  - Хозяин vs риелтор: комиссия риелтора вычитается из yield
  - Анализ торга: скрипт сравнивает с аналогами в БД (без DeepSeek)
  - Скор ЖК: год постройки + поля для расширения
"""
from __future__ import annotations
import re
import logging
from typing import Any

logger = logging.getLogger(__name__)

# ── Комиссия риелтора ─────────────────────────────────────────────────────────
REALTOR_FEE_PCT = 0.01        # 1% от стоимости
REALTOR_FEE_MIN = 400_000     # минимум 400к ₸


def realtor_fee(price: int) -> int:
    return max(int(price * REALTOR_FEE_PCT), REALTOR_FEE_MIN)


# ── Скор ЖК ───────────────────────────────────────────────────────────────────
def complex_score(year_built: int | None, complex_name: str | None) -> tuple[int, str]:
    """
    Базовый скор ЖК (0-15).
    Позже: подтягивать из таблицы жк_scores (застройщик, отзывы, КСК и т.д.)
    """
    if not year_built:
        # Попытаемся извлечь год из названия
        if complex_name:
            m = re.search(r"20(1[5-9]|2[0-9])", complex_name)
            if m:
                year_built = int(m.group())

    if not year_built:
        return 5, "год не определён"

    if year_built >= 2022:
        return 15, f"новый ЖК {year_built}"
    if year_built >= 2018:
        return 11, f"ЖК {year_built}, хорошее состояние"
    if year_built >= 2012:
        return 7, f"ЖК {year_built}, среднее состояние"
    return 4, f"старый ЖК {year_built}"


# ── Этаж ──────────────────────────────────────────────────────────────────────
def floor_score(floor: int | None, floors_total: int | None) -> tuple[int, str]:
    """Штраф за 1й и последний этаж (0-8)."""
    if not floor or not floors_total:
        return 4, "этаж не указан"
    if floor == 1:
        return 0, "1й этаж — штраф ликвидности"
    if floor == floors_total:
        return 1, f"последний {floor}/{floors_total} — штраф"
    if floor == floors_total - 1 and floors_total > 5:
        return 5, f"предпоследний {floor}/{floors_total}"
    if 2 <= floor <= 7:
        return 8, f"этаж {floor}/{floors_total} — оптимум"
    return 6, f"этаж {floor}/{floors_total}"


# ── Метраж и тип квартиры ─────────────────────────────────────────────────────
def apt_type_score(rooms: int | None, area: float | None) -> tuple[int, str]:
    """Ликвидность по комнатности и площади (0-15)."""
    reasons = []
    score = 8

    if rooms == 1:
        score = 15
        reasons.append("1к — макс спрос")
    elif rooms == 2:
        score = 13
        reasons.append("2к — стабильно")
    elif rooms == 3:
        score = 8
        reasons.append("3к — медленнее")
    elif rooms and rooms >= 4:
        score = 4
        reasons.append("4к+ — узкий спрос")

    if area:
        if area > 100:
            penalty = min(6, int((area - 100) / 10))
            score = max(score - penalty, 1)
            reasons.append(f"{area:.0f}м² — штраф ликвидности")
        elif area > 70:
            score = max(score - 2, 2)
            reasons.append(f"{area:.0f}м² — чуть выше нормы")
        elif 35 <= area <= 55:
            reasons.append(f"{area:.0f}м² оптимум")

    return score, ", ".join(reasons) if reasons else "тип не определён"


# ── Yield с учётом реальной аренды и комиссии риелтора ───────────────────────
def yield_score(
    price: int,
    monthly_rent: int | None,
    is_owner: bool | None,
) -> tuple[int, str, float]:
    """
    Считает yield с учётом реальной аренды из rental_index.
    Если продаёт риелтор — вычитает его комиссию из цены покупки (увеличивает реальную стоимость).
    Возвращает (score, reason, yield_pct).
    """
    if not monthly_rent or not price or price <= 0:
        return 0, "нет данных аренды", 0.0

    effective_price = price
    agent_note = ""
    if is_owner is False:  # риелтор
        fee = realtor_fee(price)
        effective_price = price + fee
        agent_note = f" +{fee//1000}к комиссия риелтора"

    annual_rent = monthly_rent * 12
    yield_pct = round(annual_rent / effective_price * 100, 1)
    payback = round(effective_price / annual_rent, 1) if annual_rent > 0 else 0

    if yield_pct >= 13:
        score = 20
    elif yield_pct >= 10:
        score = 15
    elif yield_pct >= 8:
        score = 10
    elif yield_pct >= 5:
        score = 5
    else:
        score = 2

    reason = f"Yield {yield_pct}%, окупаемость {payback} лет{agent_note}"
    return score, reason, yield_pct


# ── Сравнение с рынком (цена за м²) ──────────────────────────────────────────
def price_vs_market_score(
    price_per_m2: float,
    district_avg_m2: float | None,
) -> tuple[int, str]:
    """(0-15) Насколько цена ниже/выше медианы по району."""
    if not district_avg_m2:
        return 5, "нет данных по рынку"
    diff = (price_per_m2 - district_avg_m2) / district_avg_m2
    if diff < -0.15:
        return 15, f"на {abs(diff)*100:.0f}% ниже рынка 🔥"
    if diff < -0.05:
        return 10, f"на {abs(diff)*100:.0f}% ниже рынка"
    if diff < 0.05:
        return 5, "на уровне рынка"
    return 2, f"на {diff*100:.0f}% выше рынка"


# ── Анализ торга ──────────────────────────────────────────────────────────────
def bargain_analysis(
    price: int,
    area: float | None,
    district: str | None,
    rooms: int | None,
    comparables: list[dict],  # аналоги из БД
) -> dict:
    """
    Скрипт анализирует аналоги и рекомендует цену торга.
    comparables — список {'price': int, 'area': float, 'days_on_market': int}
    """
    if not comparables:
        return {"recommendation": "нет аналогов", "target_price": None, "discount_pct": 0}

    prices = [c["price"] for c in comparables if c.get("price")]
    if not prices:
        return {"recommendation": "нет данных", "target_price": None, "discount_pct": 0}

    import statistics
    median_price = statistics.median(prices)
    min_price = min(prices)

    # Долго висящие объявления торгуются лучше
    old_listings = [c for c in comparables if c.get("days_on_market", 0) > 30]
    avg_discount = 0.05  # базовый дисконт 5%
    if len(old_listings) > len(comparables) / 2:
        avg_discount = 0.08  # много старых → рынок стоит, торгуемся агрессивнее

    target = int(min(median_price, price) * (1 - avg_discount))
    discount_pct = round((price - target) / price * 100, 1)

    if price <= median_price * 0.95:
        rec = "цена уже ниже рынка, торговаться сложно"
        target = int(price * 0.97)
        discount_pct = 3.0
    elif price > median_price * 1.1:
        rec = f"переоценена на {((price/median_price)-1)*100:.0f}%, торгуйся смело"
        avg_discount = 0.10
        target = int(median_price * 0.97)
        discount_pct = round((price - target) / price * 100, 1)
    else:
        rec = f"на уровне рынка, реальный торг ~{int(avg_discount*100)}%"

    return {
        "recommendation": rec,
        "target_price": target,
        "discount_pct": discount_pct,
        "median_comparable": int(median_price),
        "comparables_count": len(comparables),
    }


# ── Локация ────────────────────────────────────────────────────────────────────
_LOC_SCORES = {
    "есиль": (20, "деловой центр"),
    "алматы": (16, "развитая инфраструктура"),
    "сарыарка": (12, "спальный, стабильный"),
    "байконур": (10, "спальный, растущий"),
    "нура": (8, "окраина"),
}
_POI_KW = ["трц", "тц", "бц", "университет", "expo", "хан шатыр",
           "мега", "больниц", "госпитал", "метро", "lrt"]


def location_score(district: str, address: str, title: str) -> tuple[int, str]:
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


# ── Главная функция ────────────────────────────────────────────────────────────
def compute_apartment_score_v2(
    listing: dict[str, Any],
    monthly_rent: int | None = None,       # из rental_index
    district_avg_m2: float | None = None,  # медиана цены продажи по району
    same_complex_count: int = 1,
    comparables: list[dict] | None = None, # аналоги для анализа торга
) -> dict[str, Any]:
    """
    Скоринг квартиры v2. Возвращает dict с total_score, breakdown, reasons, bargain.

    Веса (итого 100):
      yield          20  — реальный доход
      price_market   15  — цена vs рынок
      location       20  — район + POI
      apt_type       15  — комнатность + метраж
      floor           8  — этаж
      complex        15  — ЖК (год постройки)
      supply          7  — дефицит предложения
    """
    price = listing.get("price", 0) or 0
    area = listing.get("area")
    rooms = listing.get("rooms")
    floor = listing.get("floor")
    floors_total = listing.get("floors_total")
    district = listing.get("district", "")
    address = listing.get("address", "")
    title = listing.get("title", "")
    year_built = listing.get("year_built")
    complex_name = listing.get("complex_name", "")
    is_owner = listing.get("is_owner")  # True/False/None

    price_m2 = price / area if (area and area > 0) else 0

    s_yield, r_yield, yield_pct = yield_score(price, monthly_rent, is_owner)
    s_pm, r_pm = price_vs_market_score(price_m2, district_avg_m2)
    s_loc, r_loc = location_score(district, address, title)
    s_apt, r_apt = apt_type_score(rooms, area)
    s_floor, r_floor = floor_score(floor, floors_total)
    s_complex, r_complex = complex_score(year_built, complex_name)

    # Supply
    if same_complex_count <= 3:
        s_supply, r_supply = 7, f"дефицит ({same_complex_count} в ЖК)"
    elif same_complex_count <= 8:
        s_supply, r_supply = 4, f"умеренно ({same_complex_count} в ЖК)"
    else:
        s_supply, r_supply = 1, f"много ({same_complex_count} в ЖК)"

    total = s_yield + s_pm + s_loc + s_apt + s_floor + s_complex + s_supply

    # Анализ торга
    bargain = bargain_analysis(price, area, district, rooms, comparables or [])

    # Источник: хозяин или риелтор
    owner_note = ""
    if is_owner is True:
        owner_note = "хозяин (без комиссии)"
    elif is_owner is False:
        owner_note = f"риелтор (~{realtor_fee(price)//1000}к комиссия)"

    return {
        "total_score": min(total, 100),
        "yield_pct": yield_pct,
        "monthly_rent_used": monthly_rent,
        "breakdown": {
            "yield": s_yield,
            "price_market": s_pm,
            "location": s_loc,
            "apt_type": s_apt,
            "floor": s_floor,
            "complex": s_complex,
            "supply": s_supply,
        },
        "reasons": [r_yield, r_pm, r_loc, r_apt, r_floor, r_complex, r_supply],
        "owner_note": owner_note,
        "bargain": bargain,
    }


APT_MIN_ALERT_SCORE = 65
APT_MAX_ALERTS = 5
