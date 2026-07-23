"""
Анализ торга — реальное сравнение с аналогами из БД.

Логика:
  Берём аналоги из apartment_listings:
  - тот же район
  - те же комнаты  
  - площадь ±15% (не ±20% — чтобы не сравнивать 30м² с 55м²)
  - активные за последние 60 дней
  - не дубли

  Считаем:
  - медианную цену аналогов
  - сколько дней на рынке (last_seen - first_seen)
  - процент объявлений которые висят 30+ дней (рынок стоит)
  - рекомендованную цену торга
"""
from __future__ import annotations
import statistics
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


async def get_comparables(
    district: str | None,
    rooms: int | None,
    area: float | None,
    current_price: int,
    exclude_id: str | None = None,
) -> list[dict]:
    """Найти аналоги в БД."""
    from bot.db.pg import fetch

    if not area or area <= 0:
        area_min, area_max = 0, 999
    else:
        area_min = area * 0.85
        area_max = area * 1.15

    rows = await fetch(
        """
        SELECT id, price, area, floor, floors_total,
               first_seen, last_seen, complex_name, district, address
        FROM apartment_listings
        WHERE ($1::text IS NULL OR district ILIKE '%' || $1 || '%')
          AND ($2::int IS NULL OR rooms = $2)
          AND area BETWEEN $3 AND $4
          AND price > 0
          AND price < 200000000
          AND (is_duplicate IS NULL OR is_duplicate = FALSE)
          AND ($5::text IS NULL OR id != $5)
        ORDER BY last_seen DESC NULLS LAST
        LIMIT 30
        """,
        district, rooms, area_min, area_max, exclude_id,
    )
    return [dict(r) for r in rows]


URGENT_BONUS = 300_000  # доп. торг, если продавец сам пометил объявление «Срочно, торг»


def analyze_bargain(
    price: int,
    comparables: list[dict],
    is_owner: bool | None = None,
    is_urgent: bool = False,
) -> dict:
    """
    Анализ торга на основе реальных аналогов.

    Возвращает:
      recommendation  — текст рекомендации
      target_price    — целевая цена после торга
      discount_pct    — рекомендуемый дисконт %
      median_price    — медиана по аналогам
      min_price       — минимум по аналогам
      comparables_cnt — кол-во аналогов
      market_status   — 'hot' | 'normal' | 'cold'
      days_on_market  — дней на рынке у текущего объявления (если есть)
    """
    if not comparables:
        return {
            "recommendation": "нет аналогов для сравнения",
            "target_price": None,
            "discount_pct": 0,
            "median_price": None,
            "comparables_cnt": 0,
            "market_status": "unknown",
        }

    prices = [c["price"] for c in comparables if c.get("price")]
    if not prices:
        return {"recommendation": "нет данных по ценам", "discount_pct": 0, "comparables_cnt": 0}

    median_price = int(statistics.median(prices))
    min_price = min(prices)
    now = datetime.now(timezone.utc)

    # Доля объявлений которые висят 30+ дней
    old_count = 0
    for c in comparables:
        first = c.get("first_seen")
        last = c.get("last_seen")
        if first and last:
            if hasattr(first, 'tzinfo') and first.tzinfo is None:
                first = first.replace(tzinfo=timezone.utc)
            if hasattr(last, 'tzinfo') and last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            days = (last - first).days
            if days >= 30:
                old_count += 1

    old_ratio = old_count / len(comparables)

    # Определить температуру рынка
    if old_ratio > 0.5:
        market_status = "cold"     # рынок стоит — торгуемся агрессивно
        base_discount = 0.08
    elif old_ratio > 0.25:
        market_status = "normal"
        base_discount = 0.05
    else:
        market_status = "hot"      # всё быстро уходит — торг минимальный
        base_discount = 0.02

    # Позиция текущей цены относительно рынка
    price_ratio = price / median_price

    if price_ratio < 0.93:
        # Цена уже ниже рынка
        recommendation = f"цена ниже рынка на {(1-price_ratio)*100:.0f}% — торговаться сложно"
        discount_pct = 1.0
        target_price = int(price * 0.99)
    elif price_ratio < 1.0:
        # Цена чуть ниже медианы
        recommendation = f"цена немного ниже рынка, торг ~{int(base_discount*100)}%"
        discount_pct = base_discount * 100
        target_price = int(price * (1 - base_discount))
    elif price_ratio < 1.10:
        # На уровне рынка
        recommendation = f"цена на уровне рынка, реальный торг {int(base_discount*100)}-{int(base_discount*100+3)}%"
        discount_pct = base_discount * 100
        target_price = int(price * (1 - base_discount))
    else:
        # Переоценена
        overpriced_pct = (price_ratio - 1) * 100
        discount_pct = min(overpriced_pct + base_discount * 100, 20)
        target_price = int(median_price * 0.97)
        recommendation = f"переоценена на {overpriced_pct:.0f}% — торгуйся до {target_price:,} ₸"

    # Бонус если продаёт риелтор — у него больше мотивация закрыть сделку
    if is_owner is False:
        discount_pct = min(discount_pct + 1, 20)
        recommendation += " (риелтор — есть пространство)"

    # Продавец сам пометил объявление «Срочно, торг» — явный сигнал
    # готовности уступить сверх обычного расчёта.
    if is_urgent and price > 0:
        target_price = max(target_price - URGENT_BONUS, int(price * 0.5))
        discount_pct = round((1 - target_price / price) * 100, 1)
        recommendation += " · продавец пометил «Срочно, торг» — минус ещё 300 тыс ₸"

    return {
        "recommendation": recommendation,
        "target_price": target_price,
        "discount_pct": round(discount_pct, 1),
        "median_price": median_price,
        "min_price": min_price,
        "comparables_cnt": len(comparables),
        "market_status": market_status,
        "old_listings_ratio": round(old_ratio * 100),
    }
