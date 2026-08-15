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

## Семантика итогового 0-100 (Location Reliability Phase, задача
2026-08-15, коммит "Семантика + групповая модель")

Итог — НЕ "сумма всех adj, отнормированная в единый линейный диапазон".
Раньше было именно так (`_TOTAL_ADJ_MIN`/`_TOTAL_ADJ_MAX`, единый
глобальный диапазон) — и это дало живой баг: когда 2026-08-15 добавили
school_access/kindergarten_access, диапазон вырос (24 -> 30), и
нормализованный score ВСЕХ уже посчитанных ЖК просел (пример из
регресс-теста: 66 -> 55) чисто из-за расширения знаменателя, а не
потому что для конкретного ЖК что-то реально изменилось. Это нарушает
базовый принцип: возможность измерить что-то новое не должна задним
числом ухудшать оценку того, для чего это новое ничего не меняет.

Теперь: **взвешенное среднее по группам** (`_GROUP_WEIGHTS` ниже; веса
структурные — отражают важность категории для ЖК как таковой, НЕ
зависят от того, сколько именно факторов сейчас заведено внутри
категории):

  transport 25% / infra 25% / green 20% / noise 15% / risk 15%

Внутри группы факторы по-прежнему складываются и нормализуются по
диапазону (см. `_FACTOR_RANGES`) — но диапазон ГРУППЫ, не всей модели
целиком. Задуманный смысл шкалы: 50 — локация НИ ХУЖЕ, НИ ЛУЧШЕ среднего
по каждой измеримой оси, НЕ "у неё есть половина возможных удобств";
100 — максимум сразу по всем пяти категориям (на практике почти не
бывает).

**Известные ограничения (снимаются следующими коммитами этой же
фазы)**:

1. Диапазон группы по-прежнему СТАТИЧЕСКИЙ (Σ min/max факторов внутри
   неё), а не "диапазон только измеренных для ЭТОГО ЖК факторов".
   Значит добавление нового фактора ВНУТРИ группы (например, ещё один
   инфра-сигнал в будущем) всё ещё может немного сдвинуть нормализованный
   score группы для ЖК, где этот новый фактор не посчитан (unknown) —
   тот же баг класса 66->55, но теперь ограниченный ОДНОЙ группой (25%
   веса максимум), а не всей моделью сразу.
2. "50 = нейтрально" — ЦЕЛЕВОЙ смысл шкалы, но пока НЕ буквально верно
   для групп с неотрицательным диапазоном (transport 0..11, infra
   0..15, green 0..2): unknown-фактор там по умолчанию даёт adj=0, а
   0 — это МИНИМУМ диапазона (0%), не середина. Т.е. "не измерено" пока
   визуально неотличимо от "измерено и оказалось худшим из возможных" —
   ровно то, что закрывает confidence-redesign ниже.

Оба пункта закрываются одним и тем же следующим коммитом фазы —
"Confidence" (не считать unknown-факторы в диапазон группы вообще, не
просто как 0), закреплённым stability-тестом (последний коммит фазы,
регресс ±2 балла для существующих ЖК при добавлении нейтрального
фактора).
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

# ── Групповая модель (см. докстринг выше) ───────────────────────────────
# Веса групп — структурная константа продукта, требует явного решения
# заказчика для пересмотра (тот же тип обязательства, что было у
# _TOTAL_ADJ_MIN/MAX).
_GROUP_WEIGHTS: dict[str, float] = {
    "transport": 0.25,
    "infra": 0.25,
    "green": 0.20,
    "noise": 0.15,
    "risk": 0.15,
}

# Канонический источник группировки факторов — используется и здесь
# (normalize_group_weighted), и complex_location_score_snapshot.py
# (breakdown/group-суммы в UI) — тот файл ИМПОРТИРУЕТ эти константы
# отсюда, не дублирует их (единый источник правды). building_age УБРАН
# отсюда с 2026-08-15 ("Location Reliability Phase", коммит "двойные
# школы + building_age") — качество здания, не локации, см. докстринг
# _building_age_factor() ниже.
_GROUPS: dict[str, tuple[str, ...]] = {
    "transport": ("transit_stops", "lrt_access", "road_access", "route_connectivity"),
    "infra": ("schools", "amenities", "school_access", "kindergarten_access"),
    "noise": ("noise",),
    "green": ("parks",),
    "risk": ("demolition",),
}
_INFORMATIONAL: tuple[str, ...] = ("bank",)

# Диапазон (min, max) КАЖДОГО фактора — раньше жил только в комментарии
# (документация), теперь ещё и в коде: normalize_group_weighted() строит
# из этого диапазон группы (Σ по факторам группы). Если диапазон
# отдельного слоя изменится (score_layers/*.py) — эту таблицу и
# _GROUPS надо обновить вручную (тот же тип обязательства, что несёт
# _CLASS_SCORE в hedonic_constants.py).
#
# "schools" — 0..2, НЕ 0..5 (было до 2026-08-15): с этой задачи ("двойные
# школы + building_age") OSM-слой schools зовётся с university_only=True
# в подавляющем большинстве случаев (astana_schools/kindergartens почти
# всегда доступны, см. bot/score_layers/schools.py докстринг) — реальный
# диапазон ЭТОГО фактора В КОНТЕКСТЕ location_score теперь "вуз рядом или
# нет" (0/2), школьно-садиковая часть (была 3/5) переехала в school_
# access/kindergarten_access. В редком fallback-случае (astana-таблицы
# недоступны) OSM теоретически может вернуть до 5 — диапазон здесь
# намеренно отражает ОБЫЧНЫЙ, не крайний случай (тот же принцип, что уже
# был у building_age "год неизвестен" — см. общий комментарий про
# известные ограничения статических диапазонов в докстринге модуля).
_FACTOR_RANGES: dict[str, tuple[int, int]] = {
    "noise": (-6, 0),                  # score_layers/noise.py
    "schools": (0, 2),                 # score_layers/schools.py, university_only=True в обычном случае
    "transit_stops": (0, 3),           # score_layers/transit.py
    "amenities": (0, 4),               # score_layers/amenities.py
    "parks": (0, 2),                   # score_layers/parks.py
    "lrt_access": (0, 4),              # _transport_hex_factors
    "road_access": (0, 2),             # _transport_hex_factors
    "route_connectivity": (0, 2),      # _transport_hex_factors
    "demolition": (-2, 0),             # _demolition_factor
    "school_access": (0, 4),           # _schools_factor — задача 2026-08-15
    "kindergarten_access": (0, 2),     # _kindergartens_factor — задача 2026-08-15
    "bank": (0, 0),                    # _bank_factor — всегда 0, информационный
}


def _group_range(group: str) -> tuple[int, int]:
    keys = _GROUPS[group]
    return (sum(_FACTOR_RANGES[k][0] for k in keys), sum(_FACTOR_RANGES[k][1] for k in keys))


def normalize_group_weighted(factors: dict) -> int:
    """0-100 — см. докстринг модуля "Семантика итогового 0-100". Заменяет
    старую линейную нормализацию по единому _TOTAL_ADJ_MIN/MAX (убраны).
    Чистая функция от factors (без сети/БД) — тестируется напрямую.

    `factors` — {key: {"adj": int, ...}} с любым подмножеством ключей из
    _FACTOR_RANGES (отсутствующие ключи внутри группы просто не
    учитываются в raw-сумме ЭТОГО прогона — но диапазон группы всё равно
    статический, см. "известное ограничение" в докстринге модуля)."""
    total_pct = 0.0
    for group, weight in _GROUP_WEIGHTS.items():
        keys = _GROUPS[group]
        raw = sum(factors[k]["adj"] for k in keys if k in factors)
        lo, hi = _group_range(group)
        pct = 50.0 if hi == lo else 100.0 * (raw - lo) / (hi - lo)
        total_pct += weight * pct
    return round(total_pct)


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
    """НЕ вызывается из compute_complex_location_score() с 2026-08-15
    ("Location Reliability Phase", коммит "двойные школы + building_age")
    — возраст здания это качество ЗДАНИЯ, не локации: два соседних дома
    (2025 и 1980 года) на одной и той же точке карты должны иметь
    ОДИНАКОВЫЙ location score, что было не так, пока building_age жил в
    "risk"-группе. Функция сохранена как есть (не удалена) — прямой
    кандидат для будущего property_score/structural-quality скора,
    который считается ПО ЖК/КВАРТИРЕ, не по локации."""
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
    аудита координат ЖК).

    `year_built` — параметр СОХРАНЁН в сигнатуре ради обратной
    совместимости с вызывающими (complex_location_score_snapshot.py,
    /admin/api/complex/{id}/location-score в terminal_extras.py — их
    менять не требовалось), но с 2026-08-15 ("Location Reliability
    Phase", коммит "двойные школы + building_age") ВНУТРИ этой функции
    не используется вовсе — building_age убран из location score (см.
    докстринг _building_age_factor() выше, почему)."""
    if not lat or not lon:
        return None
    listing = {"lat": lat, "lon": lon}
    factors: dict[str, dict] = {}

    # Точные DB-факторы (не Overpass — общий pg pool, дешёво) идут ПЕРВЫМИ,
    # раньше OSM-слоёв ниже — их результат нужен, чтобы решить, в каком
    # режиме звать OSM-слой "schools" (см. ниже про double-counting).
    hex_factors, demolition_result, schools_result, kindergartens_result = await asyncio.gather(
        _transport_hex_factors(lat, lon),
        _demolition_factor(lat, lon),
        _schools_factor(lat, lon),
        _kindergartens_factor(lat, lon),
    )
    factors["lrt_access"] = {**hex_factors["lrt_access"], "label": "🚈 ЛРТ рядом"}
    factors["road_access"] = {**hex_factors["road_access"], "label": "🚗 Доступность на авто"}
    factors["route_connectivity"] = {**hex_factors["route_connectivity"], "label": "🔀 Маршрутная связность"}
    factors["demolition"] = {**demolition_result, "label": "🚧 Снос по соседству"}
    # school_access/kindergarten_access — задача 2026-08-15, ТОЧНЫЙ сигнал
    # по расстоянию+типу (astana_schools/astana_kindergartens) —
    # PRIMARY-источник, вытесняет школьно-садиковую часть OSM-слоя
    # "schools" ниже (university_only=True), не дублирует её (см.
    # докстринг bot/score_layers/schools.py про двойное взвешивание,
    # задача "Location Reliability Phase", коммит "двойные школы +
    # building_age").
    factors["school_access"] = {**schools_result, "label": "🏫 Школа рядом"}
    factors["kindergarten_access"] = {**kindergartens_result, "label": "🧸 Садик рядом"}
    # "Нет данных" здесь практически недостижимо (astana_schools/
    # kindergartens — стабильные городские справочники, 160/131 строка на
    # 2026-08-15, запрос падает только при реальном сбое БД) — но именно
    # для ЭТОГО редкого случая OSM-слой ниже остаётся полноценным fallback
    # (university_only=False), а не университетским огрызком.
    schools_precise_available = (
        "нет данных" not in schools_result["reason"] or "нет данных" not in kindergartens_result["reason"]
    )

    # transit/amenities/parks/schools все используют ОДИН и тот же shared-
    # запрос bot/score_layers/poi.py (кэш-ключ "poi700") — если их не
    # разогнать concurrently ДО прогрева кэша, каждый бьёт в Overpass
    # отдельно (лишние запросы разом). У Overpass с этого сервера реально
    # жив только 1 из 4 зеркал (см. комментарий в bot/score_layers/osm.py) —
    # лишняя параллельная нагрузка повышает риск словить рейт-лимит и
    # свалиться в каскад из гарантированно мёртвых зеркал (по 30с
    # таймаута каждое). Поэтому сначала прогреваем poi-кэш ОДНИМ запросом.
    from bot.score_layers.poi import fetch_poi
    try:
        await fetch_poi(lat, lon)
    except Exception as exc:
        logger.warning("location_score poi prefetch failed: %s", exc)

    async def _run_layer(key, module):
        try:
            if key == "schools":
                adj, reason = await module.compute(listing, university_only=schools_precise_available)
            else:
                adj, reason = await module.compute(listing)
        except Exception as exc:
            logger.warning("location_score layer %s failed: %s", key, exc)
            adj, reason = 0, f"ошибка слоя: {exc}"
        return key, adj, reason

    results = await asyncio.gather(*(_run_layer(key, module) for key, _, module in _OSM_LAYERS))
    label_by_key = {key: label for key, label, _ in _OSM_LAYERS}
    for key, adj, reason in results:
        factors[key] = {"adj": adj, "label": label_by_key[key], "reason": reason}

    factors["bank"] = {**_bank_factor(district), "label": "🌉 Берег Ишима"}

    total = sum(f["adj"] for f in factors.values())
    # Confidence — доля факторов, реально посчитанных не по дефолту/ошибке
    # (см. тот же принцип в bot/core/deal_score.py — "низкий confidence =
    # доверяем меньше").
    computed = sum(1 for f in factors.values() if "ошибка" not in f["reason"] and "нет данных" not in f["reason"])
    confidence = round(100 * computed / len(factors))

    return {"total": total, "factors": factors, "confidence": confidence}
