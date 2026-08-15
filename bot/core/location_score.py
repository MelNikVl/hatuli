"""
Локационный скор ЖК (backlog #31) — БЕЗ Yandex/2GIS API (доступа к ним нет,
явное решение пользователя: "без них всё что можно"). Собран целиком из
данных, которые уже есть в проекте бесплатно:

  - bot/score_layers/{noise,schools,transit,amenities,parks} — уже активные
    per-listing слои на чистом OSM Overpass (см. bot/score_layers/__init__.py,
    compute_all_layers, вызывается из service_apartments.py при парсинге).
    Мы ПЕРЕИСПОЛЬЗУЕМ их as-is через синтетический {"lat":.., "lon":..} —
    ноль дублирования логики/запросов.
  - hype_tracker/transport_hexes.py — уже посчитанная per-hex (100м) таблица
    transport_hexes (LRT-станции, автобусные маршруты, дороги, развязки) —
    даём 3 доп. фактора без единого нового Overpass-запроса.
  - demolition_houses (снос/реновация 2026-2030, см. /admin/analytics/demolition)
    — рядом стройплощадка = временный шум/пыль, штраф.
  - complexes.year_built — новостройка после 2015 = бонус (мировая практика,
    см. заметку в Notion "Дипсик": "доля новых домов — индикатор престижности").
  - district → берег Ишима — грубая эвристика по факту создания района Есиль
    на левом берегу в 2008г.; только информационно (adj=0), реальных данных
    о "престижности берега" для калибровки веса нет.

15-факторная модель из бенчмарка (Walk Score/AreaVibes/JLL LQS, см. Notion
"Слой локации") требует части данных, которых без Yandex/2GIS/платных API
физически нет (рейтинги школ, шум по децибелам, реальный трафик, доход
жителей) — пропущены. Это даёт честную ПОДмножество из ~12 факторов вместо
громкого "15" с фейковыми числами.
"""
from __future__ import annotations

import asyncio
import logging

from bot.score_layers import noise, schools, transit, amenities, parks

logger = logging.getLogger(__name__)

# Порядок = порядок отображения в UI.
_OSM_LAYERS = [
    ("noise", "🔇 Шум (магистрали)", noise),
    ("schools", "🏫 Школы/садики/вузы", schools),
    ("transit_stops", "🚏 Остановки рядом", transit),
    ("amenities", "🛒 Магазины/сервисы", amenities),
    ("parks", "🌳 Парки/зелень", parks),
]

_LEFT_BANK_DISTRICTS = {"есиль", "есильский"}

# Теоретический диапазон total = Σadj по ВСЕМ факторам ниже — используется
# ТОЛЬКО complex_location_score_snapshot.py (Фаза L1, docs/location_
# product_design.md §7, задача 2026-08-14) для нормализации в 0-100 при
# записи в complex_location_scores. compute_complex_location_score() САМА
# эту нормализацию не делает и возвращает total как есть (см. РЕШЕНИЕ 2
# плана L1 — живой /admin/api/complex/{id}/location-score не меняется,
# complex_detail.html:645 читает сырой total напрямую).
#
# Если диапазон отдельного слоя изменится (score_layers/*.py) — эти две
# константы надо пересчитать вручную (тот же тип обязательства, что уже
# несёт _CLASS_SCORE в hedonic_constants.py):
#   noise            -6..0   (score_layers/noise.py)
#   schools            0..5  (score_layers/schools.py)
#   transit_stops      0..3  (score_layers/transit.py)
#   amenities          0..4  (score_layers/amenities.py)
#   parks              0..2  (score_layers/parks.py)
#   lrt_access         0..4  (_transport_hex_factors)
#   road_access        0..2  (_transport_hex_factors)
#   route_connectivity 0..2  (_transport_hex_factors)
#   building_age       0..2  (_building_age_factor)
#   demolition        -2..0  (_demolition_factor)
#   bank               0..0  (_bank_factor — всегда 0, информационный)
_TOTAL_ADJ_MIN = -8
_TOTAL_ADJ_MAX = 24


async def _transport_hex_factors(lat: float, lon: float) -> dict:
    """3 доп. фактора из уже посчитанной transport_hexes (LRT/дороги/
    маршрутная связность) — ближайший гекс по простому bbox+ORDER BY."""
    from bot.db.pg import fetchrow
    try:
        row = await fetchrow("""
            SELECT score, dist_lrt, dist_bus, dist_road, dist_junction, route_count
            FROM transport_hexes
            WHERE lat BETWEEN $1 - 0.01 AND $1 + 0.01
              AND lon BETWEEN $2 - 0.016 AND $2 + 0.016
            ORDER BY (lat - $1)^2 + (lon - $2)^2
            LIMIT 1
        """, lat, lon)
    except Exception as exc:
        logger.warning("transport_hexes lookup failed: %s", exc)
        row = None

    out = {}
    if not row:
        out["lrt_access"] = {"adj": 0, "reason": "нет данных transport_hexes рядом (см. hype_tracker/transport_hexes.py — прогоняется отдельным скриптом)"}
        out["road_access"] = {"adj": 0, "reason": "нет данных"}
        out["route_connectivity"] = {"adj": 0, "reason": "нет данных"}
        return out

    dist_lrt, dist_road, dist_junction = row["dist_lrt"], row["dist_road"], row["dist_junction"]
    route_count = row["route_count"] or 0

    if dist_lrt is not None and dist_lrt <= 1000:
        adj = 4 if dist_lrt <= 400 else (2 if dist_lrt <= 700 else 1)
        out["lrt_access"] = {"adj": adj, "reason": f"ЛРТ-станция в {dist_lrt:.0f}м"}
    else:
        out["lrt_access"] = {"adj": 0, "reason": "ЛРТ дальше 1км"}

    road_ok = dist_road is not None and dist_road <= 600
    junc_ok = dist_junction is not None and dist_junction <= 800
    if road_ok and junc_ok:
        out["road_access"] = {"adj": 2, "reason": "рядом крупная дорога и развязка — удобно на машине"}
    elif road_ok:
        out["road_access"] = {"adj": 1, "reason": "рядом крупная дорога"}
    else:
        out["road_access"] = {"adj": 0, "reason": "далеко от крупных дорог"}

    if route_count >= 4:
        out["route_connectivity"] = {"adj": 2, "reason": f"{route_count} разных маршрутов рядом — легко уехать в любую сторону"}
    elif route_count >= 1:
        out["route_connectivity"] = {"adj": 1, "reason": f"{route_count} маршрут(а) рядом"}
    else:
        out["route_connectivity"] = {"adj": 0, "reason": "маршрутов рядом нет"}
    return out


async def _demolition_factor(lat: float, lon: float) -> dict:
    """Штраф, если рядом (в теории) стройплощадка под снос/реновацию —
    см. /admin/analytics/demolition. Не путать с общегородским генпланом —
    это конкретные адреса из утверждённого перечня."""
    from bot.db.pg import fetchrow
    try:
        row = await fetchrow("""
            SELECT address,
                   (((lat - $1) * 111.0)^2 + ((lon - $2) * 111.0 * 0.63)^2) AS d2
            FROM demolition_houses
            WHERE lat IS NOT NULL AND lon IS NOT NULL
            ORDER BY d2 LIMIT 1
        """, lat, lon)
    except Exception as exc:
        logger.warning("demolition_houses lookup failed: %s", exc)
        row = None
    if not row:
        return {"adj": 0, "reason": "рядом нет объектов из перечня на снос"}
    import math
    dist_m = math.sqrt(row["d2"]) * 1000
    if dist_m <= 250:
        return {"adj": -2, "reason": f"рядом дом из перечня на снос ({dist_m:.0f}м) — возможны стройка/шум в ближайшие годы"}
    return {"adj": 0, "reason": "рядом нет объектов из перечня на снос"}


_SCHOOL_BONUS_TYPES = {"лицей", "гимназия", "международная/частная", "ниш"}


async def _schools_factor(lat: float, lon: float) -> dict:
    """Точный фактор по ближайшей школе (`astana_schools`, 160 строк на
    2026-08-15: 73 общеобразовательная / 43 лицей / 35 гимназия / 7
    международная-частная / 2 НИШ) — расстояние + бонус за тип с
    углублённой программой (НИШ/международная/лицей/гимназия против
    обычной общеобразовательной, у которой бонуса нет). Бонус НЕ
    применяется, если ближайшая школа дальше 1км (базовый adj уже 0 —
    бонусировать "школа есть, но она в 5км" смысла нет).

    **Не заменяет старый OSM-фактор `schools`** (ключ "schools" в
    `_OSM_LAYERS` выше, `bot/score_layers/schools.py`) — держим оба
    осознанно: OSM-слой видит вузы (их нет в `astana_schools`, только
    школы), этот даёт более точный сигнал по расстоянию+типу для самих
    школ. Частичное двойное взвешивание школьного фактора — признанный
    компромисс, не баг; пересмотреть, когда появятся рейтинги 2GIS
    (колонка `rating` есть в `astana_schools`, но 0% заполнена на
    2026-08-15 — не используем).

    **Ограничение свежести**: в проекте нет скрипта-писателя/обновления
    для `astana_schools` (заведена вручную/внешним источником один раз,
    без таймера) — актуальность не гарантируется, в отличие от
    `transport_hexes`/`demolition_houses` выше.
    """
    from bot.db.pg import fetchrow
    try:
        row = await fetchrow("""
            SELECT type,
                   (((lat - $1) * 111.0)^2 + ((lon - $2) * 111.0 * 0.63)^2) AS d2
            FROM astana_schools
            WHERE lat IS NOT NULL AND lon IS NOT NULL
            ORDER BY d2 LIMIT 1
        """, lat, lon)
    except Exception as exc:
        logger.warning("astana_schools lookup failed: %s", exc)
        row = None
    if not row:
        return {"adj": 0, "reason": "нет данных astana_schools рядом"}

    import math
    dist_m = math.sqrt(row["d2"]) * 1000
    school_type = (row["type"] or "").strip()

    if dist_m <= 300:
        base_adj = 3
    elif dist_m <= 500:
        base_adj = 2
    elif dist_m <= 1000:
        base_adj = 1
    else:
        return {"adj": 0, "reason": f"ближайшая школа дальше 1км ({dist_m:.0f}м)"}

    if school_type.lower() in _SCHOOL_BONUS_TYPES:
        return {"adj": base_adj + 1, "reason": f"школа в {dist_m:.0f}м ({school_type}, углублённая программа)"}
    return {"adj": base_adj, "reason": f"школа в {dist_m:.0f}м ({school_type or 'тип не указан'})"}


async def _kindergartens_factor(lat: float, lon: float) -> dict:
    """Точный фактор по ближайшему садику (`astana_kindergartens`, 131
    строка на 2026-08-15). Только расстояние — без бонуса за тип: колонка
    `type` в этой таблице на 100% пустая (в отличие от `astana_schools`),
    бонусировать нечем. См. докстринг `_schools_factor()` выше про
    осознанное частичное двойное взвешивание с OSM-слоем schools и про
    ограничение свежести (нет скрипта-писателя)."""
    from bot.db.pg import fetchrow
    try:
        row = await fetchrow("""
            SELECT (((lat - $1) * 111.0)^2 + ((lon - $2) * 111.0 * 0.63)^2) AS d2
            FROM astana_kindergartens
            WHERE lat IS NOT NULL AND lon IS NOT NULL
            ORDER BY d2 LIMIT 1
        """, lat, lon)
    except Exception as exc:
        logger.warning("astana_kindergartens lookup failed: %s", exc)
        row = None
    if not row:
        return {"adj": 0, "reason": "нет данных astana_kindergartens рядом"}

    import math
    dist_m = math.sqrt(row["d2"]) * 1000

    if dist_m <= 300:
        return {"adj": 2, "reason": f"садик в {dist_m:.0f}м"}
    if dist_m <= 500:
        return {"adj": 1, "reason": f"садик в {dist_m:.0f}м"}
    return {"adj": 0, "reason": f"ближайший садик дальше 500м ({dist_m:.0f}м)"}


def _building_age_factor(year_built: int | None) -> dict:
    if not year_built:
        return {"adj": 0, "reason": "год постройки неизвестен"}
    if year_built >= 2020:
        return {"adj": 2, "reason": f"новостройка {year_built} г. — современные планировки/коммуникации"}
    if year_built >= 2015:
        return {"adj": 1, "reason": f"построен в {year_built} г. — относительно новый"}
    return {"adj": 0, "reason": f"построен в {year_built} г."}


def _bank_factor(district: str | None) -> dict:
    """Информационный, НЕ влияет на итог (adj всегда 0) — нет данных, чтобы
    обоснованно посчитать какой берег "лучше"; это грубая эвристика по
    факту застройки, не оценочное суждение."""
    d = (district or "").lower()
    if any(k in d for k in _LEFT_BANK_DISTRICTS):
        return {"adj": 0, "reason": "левый берег Ишима (р-н Есиль)"}
    if d:
        return {"adj": 0, "reason": "правый берег Ишима (исторический центр)"}
    return {"adj": 0, "reason": "район не определён"}


async def compute_complex_location_score(
    lat: float | None, lon: float | None,
    year_built: int | None = None, district: str | None = None,
) -> dict | None:
    """Итог: {"total": int, "factors": {key: {"adj","label","reason"}}}.
    None, если нет координат (ЖК с невыясненной геолокацией — см. задачу
    аудита координат ЖК)."""
    if not lat or not lon:
        return None
    listing = {"lat": lat, "lon": lon}
    factors: dict[str, dict] = {}

    # transit/amenities/parks все используют ОДИН и тот же shared-запрос
    # bot/score_layers/poi.py (кэш-ключ "poi700") — если их не разогнать
    # concurrently ДО прогрева кэша, каждый из трёх бьёт в Overpass отдельно
    # (3 лишних запроса разом). У Overpass с этого сервера реально жив
    # только 1 из 4 зеркал (см. комментарий в bot/score_layers/osm.py) —
    # лишняя параллельная нагрузка повышает риск словить рейт-лимит и
    # свалиться в каскад из 3 гарантированно мёртвых зеркал (по 30с
    # таймаута каждое). Поэтому сначала прогреваем poi-кэш ОДНИМ запросом.
    from bot.score_layers.poi import fetch_poi
    try:
        await fetch_poi(lat, lon)
    except Exception as exc:
        logger.warning("location_score poi prefetch failed: %s", exc)

    async def _run_layer(key, module):
        try:
            adj, reason = await module.compute(listing)
        except Exception as exc:
            logger.warning("location_score layer %s failed: %s", key, exc)
            adj, reason = 0, f"ошибка слоя: {exc}"
        return key, adj, reason

    results = await asyncio.gather(*(_run_layer(key, module) for key, _, module in _OSM_LAYERS))
    label_by_key = {key: label for key, label, _ in _OSM_LAYERS}
    for key, adj, reason in results:
        factors[key] = {"adj": adj, "label": label_by_key[key], "reason": reason}

    hex_factors = await _transport_hex_factors(lat, lon)
    factors["lrt_access"] = {**hex_factors["lrt_access"], "label": "🚈 ЛРТ рядом"}
    factors["road_access"] = {**hex_factors["road_access"], "label": "🚗 Доступность на авто"}
    factors["route_connectivity"] = {**hex_factors["route_connectivity"], "label": "🔀 Маршрутная связность"}

    factors["building_age"] = {**_building_age_factor(year_built), "label": "🏗 Возраст дома"}
    factors["demolition"] = {**await _demolition_factor(lat, lon), "label": "🚧 Снос по соседству"}
    factors["bank"] = {**_bank_factor(district), "label": "🌉 Берег Ишима"}

    total = sum(f["adj"] for f in factors.values())
    # Confidence — доля факторов, реально посчитанных не по дефолту/ошибке
    # (см. тот же принцип в bot/core/deal_score.py — "низкий confidence =
    # доверяем меньше").
    computed = sum(1 for f in factors.values() if "ошибка" not in f["reason"] and "нет данных" not in f["reason"])
    confidence = round(100 * computed / len(factors))

    return {"total": total, "factors": factors, "confidence": confidence}
