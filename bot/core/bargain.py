"""
Анализ торга — реальное сравнение с аналогами из БД.

Логика (v2 — гексагональная, согласованная с deal_score.py; hex_price.py
упоминался тут исторически — архивирован, docs/archive/README.md,
константы согласования теперь в bot/core/hedonic_constants.py):
  Аналоги берём НЕ по текстовому району (слишком грубо — "Есильский р-н"
  тянет и премиальный ЖК, и старую панель в 3 км), а строго:
  - тот же гексагон + 6 соседних (тот же HEX_EDGE_M, что у Deal Score)
  - те же комнаты
  - площадь ±15%
  - тот же класс ЖК, если класс оцениваемой квартиры известен
    (если не известен — считаем без разбивки по классу и явно
    пишем это в рекомендации, а не выдаём цену "как будто узнали класс")
  - активные, не дубли, цена в разумных пределах

  Если в гексагоне+кольце с учётом класса аналогов < MIN_COMPARABLES —
  сначала пробуем ослабить фильтр по классу (включить ЖК с неизвестным
  классом), и только если данных всё равно мало — сравниваем с городским
  сегментом (те же комнаты+площадь) и явно помечаем это в meta/тексте,
  вместо того чтобы тихо смешать несравнимые районы.

  Считаем:
  - медианную цену аналогов
  - сколько дней на рынке (last_seen - first_seen)
  - процент объявлений которые висят 30+ дней (рынок стоит)
  - рекомендованную цену торга
"""
from __future__ import annotations
import math
import statistics
import logging
from datetime import datetime, timezone

from bot.core.hexgrid import hex_id, neighbors
# Задача 2026-08-14 (Часть 2, п.9: "общее hedonic-ядро для bargain.py и
# deal_score.py") — AREA_BAND_PCT/пороги больше не определяются здесь
# по-своему, импортируются из bot/core/hedonic_constants.py (та же пара,
# что deal_score.py) — раньше синхронизировались вручную комментарием,
# см. живой инцидент #1014506231 "Landmark" в docstring этого модуля.
# Локальные имена (MIN_COMPARABLES/MIN_SAME_COMPLEX) сохранены как алиасы
# — используются по всему остальному файлу, переименовывать не было нужды.
from bot.core.hedonic_constants import AREA_BAND_PCT, MIN_BLDG as MIN_SAME_COMPLEX, MIN_RING as MIN_COMPARABLES

logger = logging.getLogger(__name__)

_M_PER_DEG_LAT = 110_574.0


def _activity_filter(as_of: datetime | None, param_idx: int, alias: str = "") -> tuple[str, list]:
    """SQL-фрагмент фильтра активности для аналогов — задача 2026-08-14
    (Фаза A.5 п.1 вердикт-стратегии, docs/verdict_strategy.md): раньше НИ
    ОДИН из 4 запросов get_comparables() не фильтровал активность вовсе —
    архивные (проданные/снятые) объявления тихо участвовали в медиане
    "текущего рынка", искажая target_price/discount_pct тем сильнее, чем
    старше и дешевле давно ушедшие аналоги.

    as_of=None (по умолчанию — весь текущий вызов из попапа/парсера, живой
    "сейчас"): текущий is_active, архив не участвует.
    as_of=дата: точечная реконструкция "было активно НА ЭТУ ДАТУ"
    (first_seen <= as_of И (ещё не архивировано ИЛИ архивировано позже
    as_of)) — НЕ текущий is_active, для честного backtesting/снапшотов,
    где текущее состояние БД (объявление могло уйти в архив уже после
    интересующей даты) не совпадает с состоянием на момент, который
    анализируется.
    """
    p = f"{alias}." if alias else ""
    if as_of is None:
        return f"AND {p}is_active IS NOT FALSE", []
    return (f"AND {p}first_seen <= ${param_idx} "
            f"AND ({p}archived_at IS NULL OR {p}archived_at > ${param_idx})", [as_of])


async def get_comparables(
    lat: float | None,
    lon: float | None,
    rooms: int | None,
    area: float | None,
    current_price: int,
    complex_name: str | None = None,
    district: str | None = None,
    exclude_id: str | None = None,
    as_of: datetime | None = None,
) -> tuple[list[dict], dict]:
    """Найти аналоги: тот же гексагон+кольцо, комнаты, площадь ±15%, класс ЖК.

    as_of — см. _activity_filter(); None (по умолчанию) — текущий
    is_active, как и раньше для всех живых вызовов (попап/парсер).

    Возвращает (comparables, meta). meta:
      method      — 'same_complex' | 'hex+ring+class' | 'hex+ring' | 'city_segment' | 'district_fallback'
      class       — класс ЖК оцениваемой квартиры (или None, если не определён)
      class_note  — текст для UI, если расчёт цены не учитывает класс ЖК
    """
    from bot.db.pg import fetch

    if not area or area <= 0:
        area_min, area_max = 0, 999
    else:
        area_min = area * (1 - AREA_BAND_PCT)
        area_max = area * (1 + AREA_BAND_PCT)

    meta = {"method": "district_fallback", "class": None, "class_note": None}

    # ПРИОРИТЕТ №1 — аналоги в ТОМ ЖЕ ЖК (задача "проверить реальные цены",
    # 2026-08-13): у большинства ЖК complexes.housing_class не заполнен
    # (официальной классификации нет), из-за чего ниже логика молча мешает
    # эту квартиру с домами по соседству любого качества — а если у ЖК
    # заметно выше медиана цены/м² своих же объявлений, чем у соседей,
    # это и есть класс/качество постройки, которое просто ещё не занесли в
    # housing_class руками. Если у ЖК есть хотя бы MIN_SAME_COMPLEX своих
    # объявлений — они точнее любого гекс-соседства, используем их первым
    # делом (площадь всё ещё ±15% — сравниваем сопоставимые квартиры внутри
    # ЖК, а не студию с трёшкой).
    if complex_name:
        activity_sql, activity_params = _activity_filter(as_of, 6)
        same_complex_rows = await fetch(
            f"""
            SELECT id, price, area, floor, floors_total,
                   first_seen, last_seen, complex_name, district, address
            FROM apartment_listings
            WHERE lower(trim(complex_name)) = lower(trim($1))
              AND ($2::int IS NULL OR rooms = $2)
              AND area BETWEEN $3 AND $4
              AND price > 0 AND price < 200000000
              AND (is_duplicate IS NULL OR is_duplicate = FALSE)
              AND ($5::text IS NULL OR id != $5)
              {activity_sql}
            ORDER BY last_seen DESC NULLS LAST
            LIMIT 30
            """,
            complex_name, rooms, area_min, area_max, exclude_id, *activity_params,
        )
        if len(same_complex_rows) >= MIN_SAME_COMPLEX:
            meta["method"] = "same_complex"
            meta["class_note"] = (
                f"сравнение с {len(same_complex_rows)} объявлениями в этом же ЖК — "
                f"точнее соседей по гексагону, если у ЖК свой уровень цены"
            )
            return [dict(r) for r in same_complex_rows], meta

    if lat is None or lon is None:
        # Без координат гекс-сравнение невозможно — старый район-фоллбэк.
        activity_sql, activity_params = _activity_filter(as_of, 6)
        rows = await fetch(
            f"""
            SELECT id, price, area, floor, floors_total,
                   first_seen, last_seen, complex_name, district, address
            FROM apartment_listings
            WHERE ($1::text IS NULL OR district ILIKE '%' || $1 || '%')
              AND ($2::int IS NULL OR rooms = $2)
              AND area BETWEEN $3 AND $4
              AND price > 0 AND price < 200000000
              AND (is_duplicate IS NULL OR is_duplicate = FALSE)
              AND ($5::text IS NULL OR id != $5)
              {activity_sql}
            ORDER BY last_seen DESC NULLS LAST
            LIMIT 30
            """,
            district, rooms, area_min, area_max, exclude_id, *activity_params,
        )
        meta["class_note"] = "нет координат объявления — сравнение по району, не по гексагону"
        return [dict(r) for r in rows], meta

    from bot.db import settings as app_settings
    edge = float(app_settings.get_int("HEX_EDGE_M", 50))
    target_hex = hex_id(lat, lon, edge)
    candidate_hexes = {target_hex, *neighbors(target_hex)}

    target_class = None
    if complex_name:
        cx_row = await fetch(
            "SELECT housing_class FROM complexes WHERE lower(trim(name)) = lower(trim($1)) LIMIT 1",
            complex_name,
        )
        if cx_row and cx_row[0].get("housing_class"):
            target_class = cx_row[0]["housing_class"]
    meta["class"] = target_class
    if not target_class:
        meta["class_note"] = "класс ЖК не определён — расчёт цены без учёта класса жилья"

    # Гексагон+кольцо (7 ячеек, ребро HEX_EDGE_M) укладывается в bounding-box
    # ~edge*3 по каждой оси — фильтруем в SQL грубо (индекс по lat/lon), точную
    # принадлежность гексагону проверяем в Python (hex_id не индексируется).
    box_deg_lat = edge * 3 / _M_PER_DEG_LAT
    box_deg_lon = edge * 3 / (111_320.0 * math.cos(math.radians(lat)))

    activity_sql, activity_params = _activity_filter(as_of, 9, alias="a")
    rows = await fetch(
        f"""
        SELECT a.id, a.price, a.area, a.floor, a.floors_total, a.lat, a.lon,
               a.first_seen, a.last_seen, a.complex_name, a.district, a.address,
               c.housing_class
        FROM apartment_listings a
        LEFT JOIN complexes c ON lower(trim(c.name)) = lower(trim(a.complex_name))
        WHERE a.lat BETWEEN $1 AND $2 AND a.lon BETWEEN $3 AND $4
          AND ($5::int IS NULL OR a.rooms = $5)
          AND a.area BETWEEN $6 AND $7
          AND a.price > 0 AND a.price < 200000000
          AND (a.is_duplicate IS NULL OR a.is_duplicate = FALSE)
          AND ($8::text IS NULL OR a.id != $8)
          {activity_sql}
        """,
        lat - box_deg_lat, lat + box_deg_lat, lon - box_deg_lon, lon + box_deg_lon,
        rooms, area_min, area_max, exclude_id, *activity_params,
    )
    in_hex = [dict(r) for r in rows
              if r["lat"] is not None and r["lon"] is not None
              and hex_id(float(r["lat"]), float(r["lon"]), edge) in candidate_hexes]

    if target_class:
        same_class = [c for c in in_hex if c.get("housing_class") == target_class]
        if len(same_class) >= MIN_COMPARABLES:
            meta["method"] = "hex+ring+class"
            meta["class_note"] = None
            return same_class[:30], meta
        # мало аналогов того же класса рядом — ослабляем фильтр по классу,
        # но честно об этом пишем, а не выдаём цену за "точную по классу"
        meta["method"] = "hex+ring"
        meta["class_note"] = (
            f"рядом мало аналогов класса «{target_class}» ({len(same_class)}) — "
            f"добавлены соседние ЖК без учёта класса"
        )
        if len(in_hex) >= MIN_COMPARABLES:
            return in_hex[:30], meta
    else:
        if len(in_hex) >= MIN_COMPARABLES:
            meta["method"] = "hex+ring"
            return in_hex[:30], meta

    # Данных в гексагоне+кольце всё равно мало — сравниваем с городским
    # сегментом (те же комнаты+площадь), явно помечая это как менее точное.
    activity_sql, activity_params = _activity_filter(as_of, 5)
    city_rows = await fetch(
        f"""
        SELECT id, price, area, floor, floors_total, first_seen, last_seen,
               complex_name, district, address
        FROM apartment_listings
        WHERE ($1::int IS NULL OR rooms = $1)
          AND area BETWEEN $2 AND $3
          AND price > 0 AND price < 200000000
          AND (is_duplicate IS NULL OR is_duplicate = FALSE)
          AND ($4::text IS NULL OR id != $4)
          {activity_sql}
        ORDER BY last_seen DESC NULLS LAST
        LIMIT 30
        """,
        rooms, area_min, area_max, exclude_id, *activity_params,
    )
    meta["method"] = "city_segment"
    meta["class_note"] = (
        (meta["class_note"] + "; " if meta["class_note"] else "")
        + f"аналогов рядом (гекс+кольцо) недостаточно ({len(in_hex)}) — "
          f"сравнение по городу в том же сегменте (комнаты/площадь)"
    )
    return [dict(r) for r in city_rows], meta


URGENT_BONUS = 300_000  # доп. торг, если продавец сам пометил объявление «Срочно, торг»


def analyze_bargain(
    price: int,
    comparables: list[dict],
    is_owner: bool | None = None,
    is_urgent: bool = False,
    meta: dict | None = None,
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
      method          — как искали аналоги (см. get_comparables.meta)
      class_note      — предупреждение про класс ЖК/качество сравнения (если есть)
    """
    meta = meta or {}
    method = meta.get("method")
    class_note = meta.get("class_note")

    if not comparables:
        return {
            "recommendation": "нет аналогов для сравнения" + (f" ({class_note})" if class_note else ""),
            "target_price": None,
            "discount_pct": 0,
            "median_price": None,
            "comparables_cnt": 0,
            "market_status": "unknown",
            "method": method,
            "class_note": class_note,
        }

    prices = [c["price"] for c in comparables if c.get("price")]
    if not prices:
        return {"recommendation": "нет данных по ценам", "discount_pct": 0, "comparables_cnt": 0,
                "method": method, "class_note": class_note}

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

    if class_note:
        recommendation += f" ({class_note})"

    return {
        "recommendation": recommendation,
        "target_price": target_price,
        "discount_pct": round(discount_pct, 1),
        "median_price": median_price,
        "min_price": min_price,
        "comparables_cnt": len(comparables),
        "market_status": market_status,
        "old_listings_ratio": round(old_ratio * 100),
        "method": method,
        "class_note": class_note,
    }
