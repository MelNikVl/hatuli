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

## Пять latent-свойств локации (Location Reliability Phase v2, задача
2026-08-15, коммит "Семантика + якоря + иерархическая модель")

Философия — измерительная система, НЕ накопитель баллов: мы измеряем
ПЯТЬ фундаментальных latent-свойств локации, каждое 0-100 из своих
наблюдаемых факторов, итог — фиксированная взвешенная сумма свойств.
Новый источник данных УТОЧНЯЕТ измерение свойства, а не добавляет
бонус к общей сумме (та же логика, что у хорошего индекса инфляции:
появление нового товара в корзине не меняет инфляцию ЗАДНИМ ЧИСЛОМ за
прошлые периоды — только уточняет будущие измерения).

Раньше (первая версия этой же фазы) итог был "суммой adj в едином
линейном диапазоне" (`_TOTAL_ADJ_MIN`/`_TOTAL_ADJ_MAX`) — живой баг:
2026-08-15 добавление school_access/kindergarten_access расширило
диапазон (24 -> 30), и score ВСЕХ уже посчитанных ЖК просел (66 -> 55)
чисто из-за роста знаменателя, без единого содержательного изменения.
Групповая модель первой версии ограничила баг рамками ОДНОЙ группы —
эта (вторая) версия переопределяет сами группы как latent-СВОЙСТВА с
фиксированными долгосрочными весами и явными якорями шкалы, а не
просто "категории факторов".

**Пять свойств, их веса (`_GROUP_WEIGHTS`) и якоря 0/50/100:**

  **transport** = Accessibility, 25% — остановки/маршруты/LRT/дороги
    (позже: walkability, реальное время поездки). 0 = транспортная
    изоляция; 50 = нормальный городской уровень Астаны; 100 =
    исключительная доступность.
  **infra** = Everyday infrastructure, 25% — школы/садики/магазины
    (позже: 2GIS-рейтинги, вместимость). 0 = отсутствие повседневной
    инфраструктуры в шаговой доступности; 50 = нормальный уровень;
    100 = исключительный набор.
  **environment** = Environment, 20% — зелень + шум ОБЪЕДИНЕНЫ в одно
    свойство (было раньше два разных — green 20%+noise 15%=35%, теперь
    одно 20%); воздух (`air_quality`) — УТОЧНЕНИЕ этого же свойства,
    не отдельный бонус/группа. 0 = худшая наблюдаемая среда (шум+нет
    зелени+плохой воздух); 50 = нормальный городской уровень; 100 =
    исключительно комфортная среда.
  **risk** = Location risk, 15% — снос, будущая стройка (воздух сюда
    НЕ входит — переехал в environment, см. выше). 0 = максимальный
    зафиксированный риск; 50 = нормальный уровень (риска не выявлено);
    100 в этой группе структурно недостижим (это ШТРАФНАЯ ось, не
    бонусная — диапазоны факторов здесь ≤0).
  **urban_quality** = Urban quality/desirability, 15% — качество/
    привлекательность района как такового. **СЕЙЧАС ПУСТО** (`_GROUPS
    ["urban_quality"] = ()`, ни одного измеримого фактора) —
    confidence этого свойства ВСЕГДА 0%, Unknown ≠ average: не
    подменяем нейтральной оценкой, честно показываем "не измерено".
    `building_age` СЮДА НЕ ВХОДИТ (см. докстринг `_building_age_
    factor()` — это качество здания, не локации).

Якоря 0/50/100 ОДИНАКОВЫ по смыслу для каждого свойства (кроме risk,
структурно однонаправленного): 0 — худшее наблюдаемое состояние по
ЭТОЙ оси, 50 — НОРМАЛЬНЫЙ городской уровень Астаны (не "не знаем", а
осознанная точка отсчёта — типичный ЖК без выдающихся плюсов/минусов
по этой оси), 100 — исключительный уровень (редко достижим сразу по
всем пяти осям одновременно).

**Иерархическая нормализация**: каждое свойство — своя 0-100 шкала из
СВОИХ факторов (`normalize_group_weighted()`/`_group_pct()`), итог —
Σ(вес_свойства × pct_свойства). Добавление фактора ВНУТРИ свойства
уточняет ТОЛЬКО его 0-100, не трогая веса верхнего уровня — тот же
принцип, что убрал баг 66->55 навсегда (см. следующий абзац).

**Availability закрывает известные ограничения первой версии фазы**:
диапазон свойства строится ТОЛЬКО из факторов, реально доступных
(`_is_available`) для КОНКРЕТНОГО ЖК — неизмеренный фактор исключается
из диапазона ЦЕЛИКОМ, не считается как 0. Свойство без единого
доступного фактора вносит честные 50% (нормальный уровень по
умолчанию, не 0%/100% по случайности того, на каком конце диапазона
лежит "неизвестно") — это и есть свойство, которое проверяет
stability-тест: добавление нового фактора/свойства не должно
сдвигать score существующих ЖК, для которых оно unknown.

**Confidence — ДВУХУРОВНЕВЫЙ**, не просто "доля посчитанных факторов":
  - На уровне ФАКТОРА: `available`/`source_quality`/`freshness`/
    `precision` (см. `_SOURCE_QUALITY`/`_FRESHNESS`/`_PRECISION`
    ниже) — 0.8 точный городской реестр / 0.6 OSM Overpass / 0.2
    грубая эвристика.
  - На уровне СВОЙСТВА: `_group_confidence()` — та же логика source_
    quality, но взвешена по факторам ВНУТРИ одного свойства, не по
    всей схеме. Возвращает пару "score X/100, confidence Y%" на КАЖДОЕ
    из пяти свойств (`compute_complex_location_score()["group_scores"]`/
    `["group_confidence"]`) — "73/31%" значит "из измеренного
    получается 73, но данных мало", НЕ "уверены, что среда хорошая".
    `urban_quality` со СЕЙЧАС пустой схемой факторов даёт confidence=0
    структурно, всегда, пока не появится хотя бы один фактор.

**Calibration roadmap** (hedonic-регрессия по накопленной истории цен +
Bradley-Terry/conjoint по пользовательским предпочтениям) —
задокументирован в docs/location_product_design.md как БУДУЩАЯ работа,
явно НЕ реализуется в эту фазу (данные ещё копятся).
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

# ── Пять latent-свойств (см. докстринг выше) ────────────────────────────
# Ключи словаря — короткие внутренние имена (совпадают с исторически
# сложившимися SQL-колонками transport_score/infra_score/risk_score в
# complex_location_scores — задача 2026-08-15 v2 НЕ переименовывает эти
# колонки, чтобы не тянуть миграцию/UI-переделку ради одного захода):
#   transport      = Accessibility
#   infra           = Everyday infrastructure
#   environment     = Environment (было ДВЕ группы — green 20%+noise 15%,
#                     теперь ОДНО свойство 20%; air_quality сюда же)
#   risk            = Location risk (снос, будущая стройка; воздух УБРАН
#                     отсюда — переехал в environment как уточнение)
#   urban_quality   = Urban quality/desirability, НОВОЕ, пока пустое
#
# Веса групп — структурная константа продукта, требует явного решения
# заказчика для пересмотра (тот же тип обязательства, что было у
# _TOTAL_ADJ_MIN/MAX). ЭТО НЕ СЛУЧАЙНО: stability-тест (tests/test_
# location_score_stability.py) нашёл честную границу гарантии —
# добавление НОВОГО ФАКТОРА внутри существующей группы строго
# нейтрально (Δ=0) для ЖК, где он unknown, а вот добавление НОВОЙ
# ГРУППЫ с весом, забранным у существующей, ТАК НЕ гарантировано: если
# группа-донор веса была у своего края (100%/0%) для конкретного ЖК,
# урезание её доли неизбежно сдвигает итог. Поэтому смена весов ГРУПП
# (в отличие от добавления фактора В группу) ВСЕГДА требует ручного
# решения и явного гейта на реальных данных, не тихий рефакторинг —
# ЭТОТ пересмотр (green+noise -> environment, + urban_quality) именно
# такое решение, явно поставленное заказчиком 2026-08-15.
_GROUP_WEIGHTS: dict[str, float] = {
    "transport": 0.25,
    "infra": 0.25,
    "environment": 0.20,
    "risk": 0.15,
    "urban_quality": 0.15,
}

# Канонический источник группировки факторов — используется и здесь
# (normalize_group_weighted), и complex_location_score_snapshot.py
# (breakdown/group-суммы в UI) — тот файл ИМПОРТИРУЕТ эти константы
# отсюда, не дублирует их (единый источник правды). building_age НЕ
# входит никуда (качество здания, не локации, см. докстринг
# _building_age_factor() ниже) — в т.ч. НЕ входит в urban_quality,
# несмотря на смысловую близость названия.
_GROUPS: dict[str, tuple[str, ...]] = {
    "transport": ("transit_stops", "lrt_access", "road_access", "route_connectivity"),
    "infra": ("schools", "amenities", "school_access", "kindergarten_access"),
    # environment = green + noise (ОБЪЕДИНЕНЫ 2026-08-15 v2, были раньше
    # раздельными группами) + air_quality (переехал сюда ИЗ risk — воздух
    # уточняет среду, не является отдельным риском).
    "environment": ("noise", "parks", "air_quality"),
    "risk": ("demolition",),
    # urban_quality — НОВОЕ свойство, задача 2026-08-15 v2. Пустой tuple
    # НАМЕРЕННО — ни одного измеримого фактора нет ещё (кандидаты на
    # будущее: класс ЖК, престижность района, спрос/turnover как proxy
    # десирабельности — НЕ реализовано, Unknown ≠ average, confidence=0
    # структурно через _group_confidence() ниже, а не подмена нулём/
    # средним значением).
    "urban_quality": (),
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
    "air_quality": (-3, 0),            # _air_quality_factor — задача 2026-08-15 "воздух"
    "bank": (0, 0),                    # _bank_factor — всегда 0, информационный
}


def _group_range(group: str) -> tuple[int, int]:
    """СТАТИЧЕСКИЙ теоретический диапазон группы (Σ min/max ВСЕХ факторов
    по схеме) — только для документации/отображения "какой максимум в
    принципе возможен". normalize_group_weighted() его больше НЕ
    использует напрямую (см. _group_range_available() — динамический,
    только по факторам, реально измеренным для конкретного ЖК)."""
    keys = _GROUPS[group]
    return (sum(_FACTOR_RANGES[k][0] for k in keys), sum(_FACTOR_RANGES[k][1] for k in keys))


def _is_available(factor: dict) -> bool:
    """Реально ли фактор посчитан (не "нет данных"/"ошибка" в reason) —
    единый источник правды и для normalize_group_weighted() (какие
    факторы считаются в диапазон группы), и для confidence (какие
    факторы засчитываются как измеренные). Строковая проверка (не
    отдельный булев флаг на факторе) — обратно совместима со ВСЕМИ уже
    существующими factor-словарями, включая исторические строки
    complex_location_scores.breakdown, у которых нет никакого нового
    поля "available" вообще (задача 2026-08-15, коммит "Confidence")."""
    reason = factor.get("reason", "")
    return "нет данных" not in reason and "ошибка" not in reason


def _group_range_available(group: str, factors: dict) -> tuple[int, int] | None:
    """Диапазон группы, ограниченный ТОЛЬКО реально измеренными (available)
    факторами для КОНКРЕТНОГО ЖК — не статическая схема. None, если в
    группе вообще нет измеренных факторов (нечего нормализовать —
    normalize_group_weighted() в этом случае берёт честную середину)."""
    keys = [k for k in _GROUPS[group] if k in factors and _is_available(factors[k])]
    if not keys:
        return None
    return (sum(_FACTOR_RANGES[k][0] for k in keys), sum(_FACTOR_RANGES[k][1] for k in keys))


def _group_pct(group: str, factors: dict) -> float:
    """0.0-100.0 — положение ОДНОГО свойства на его шкале (см. докстринг
    модуля "Пять latent-свойств" — якоря 0/50/100). Вынесена из
    normalize_group_weighted() отдельной функцией (задача 2026-08-15 v2,
    коммит "Семантика + якоря + иерархическая модель") — переиспользуется
    ею же (Σ вес×pct) и _group_confidence()/внешним API для пары "score
    X/100, confidence Y%" на каждое свойство (коммит "Confidence" той же
    фазы)."""
    rng = _group_range_available(group, factors)
    if rng is None:
        return 50.0
    keys = [k for k in _GROUPS[group] if k in factors and _is_available(factors[k])]
    raw = sum(factors[k]["adj"] for k in keys)
    lo, hi = rng
    return 50.0 if hi == lo else 100.0 * (raw - lo) / (hi - lo)


def normalize_group_weighted(factors: dict) -> int:
    """0-100 — см. докстринг модуля "Пять latent-свойств". Заменяет
    старую линейную нормализацию по единому _TOTAL_ADJ_MIN/MAX (убраны).
    Чистая функция от factors (без сети/БД) — тестируется напрямую,
    переиспользуется и на исторических breakdown из complex_location_
    scores (complex_location_score_snapshot.py — не только на свежем
    выводе compute_complex_location_score()).

    `factors` — {key: {"adj": int, "reason": str, ...}} с любым
    подмножеством ключей из _FACTOR_RANGES. Положение КАЖДОГО свойства
    на его 0-100 шкале — _group_pct() — строится ДИНАМИЧЕСКИ, только из
    факторов, которые реально доступны (_is_available) для ЭТОГО набора
    factors, не из статической схемы (см. _group_range_available()).
    Свойство без единого доступного фактора вносит нейтральные 50 (=
    "нормальный городской уровень", НЕ 0)."""
    return round(sum(weight * _group_pct(group, factors) for group, weight in _GROUP_WEIGHTS.items()))


# ── Confidence (задача 2026-08-15, коммит "Confidence") ─────────────────
# source_quality — доверие к ИСТОЧНИКУ данных (не к конкретному
# измерению): точный городской реестр > OSM Overpass > грубая эвристика.
_SOURCE_QUALITY: dict[str, float] = {
    "noise": 0.6, "schools": 0.6, "transit_stops": 0.6, "amenities": 0.6, "parks": 0.6,  # OSM
    "lrt_access": 0.8, "road_access": 0.8, "route_connectivity": 0.8,                    # transport_hexes
    "demolition": 0.8,                                                                    # demolition_houses
    "school_access": 0.8, "kindergarten_access": 0.8,                                     # astana_schools/kindergartens
    "air_quality": 0.8,                                                                    # air_stations — реальный сенсор ПНЗ
    "bank": 0.2,                                                                          # грубая эвристика по district
}

# freshness — насколько регулярно обновляется ИСТОЧНИК (категория, не
# вычисляется live — ни у одной из таблиц ниже нет по-факторного
# updated_at на уровне отдельной точки, который стоило бы тащить сюда):
#   "live"     — считается заново на каждый запрос (Overpass)
#   "periodic" — обновляется по таймеру (transport_hexes)
#   "manual"   — ручной/разовый сбор без таймера (demolition_houses,
#                astana_schools/kindergartens — см. их докстринги про
#                отсутствие writer-скрипта)
#
# noise/schools/transit_stops/amenities/parks: "live" -> "periodic"
# (задача 2026-08-16, "Локальный OSM-слой") — bot/score_layers/{noise,
# schools,poi} теперь читают city_poi (наполняется city-poi-sync.timer,
# еженедельно), Overpass — только фолбэк на ещё не синхронизированную
# категорию (редкий случай, см. bot/score_layers/osm.py::local_poi_near).
# Статическая метка не различает "прямо сейчас сходили в local" от
# редкого live-фолбэка — сознательное упрощение (см. докстринг
# _apply_freshness_confidence_penalty ниже: РЕАЛЬНАЯ деградация
# уверенности идёт по фактическому возрасту city_poi.updated_at, не по
# этой метке — метка тут чисто описательная, для UI).
_FRESHNESS: dict[str, str] = {
    "noise": "periodic", "schools": "periodic", "transit_stops": "periodic",
    "amenities": "periodic", "parks": "periodic",
    "lrt_access": "periodic", "road_access": "periodic", "route_connectivity": "periodic",
    "demolition": "manual",
    "school_access": "manual", "kindergarten_access": "manual",
    "air_quality": "periodic",  # krisha-air-stations.timer, почасово
    "bank": "manual",
}

# Задача 2026-08-16 ("Локальный OSM-слой", п.4 "Graceful degradation") —
# ключи factors, чей source_quality зависит от свежести city_poi (не
# статическая константа выше — читается ФАКТИЧЕСКОЕ MIN(updated_at) по
# категориям, которыми они пользуются, см. _apply_freshness_confidence_
# penalty). demolition/*_access/air_quality/bank — НЕ отсюда: у каждого
# своя, отдельная от city_poi таблица-источник.
_OSM_LOCAL_FACTOR_KEYS = ("noise", "schools", "transit_stops", "amenities", "parks")
# kind-набор city_poi, которым СОВОКУПНО пользуются факторы выше (см.
# bot/score_layers/schools.py::_LOCAL_KINDS, poi.py::_LOCAL_KIND_MAP,
# noise.py::_LOCAL_KINDS_MAJOR/_SECONDARY) — самая старая запись СРЕДИ
# ВСЕХ этих kind определяет свежесть всей группы (см. докстринг
# city_poi_freshness_days в osm.py: берём MIN, не среднее/MAX).
_OSM_LOCAL_KINDS = [
    "school", "kindergarten", "university", "bus_stop", "shop", "mall",
    "pharmacy", "clinic", "hospital", "food", "park",
    "road_major", "road_secondary",
]
_STALE_CONFIDENCE_MULT_14D = 0.8
_STALE_CONFIDENCE_MULT_30D = 0.5

# precision — насколько детализирован сигнал:
#   "exact"     — точное расстояние/число (метры, count маршрутов)
#   "presence"  — просто да/нет в радиусе (OSM-слои)
#   "heuristic" — грубая прикидка без калибровки (bank)
_PRECISION: dict[str, str] = {
    "noise": "presence", "schools": "presence", "transit_stops": "presence", "amenities": "presence", "parks": "presence",
    "lrt_access": "exact", "road_access": "exact", "route_connectivity": "exact",
    "demolition": "exact",
    "school_access": "exact", "kindergarten_access": "exact",
    "air_quality": "exact",  # index_value — точное число, не presence/heuristic
    "bank": "heuristic",
}


def _annotate_factor_metadata(factors: dict) -> None:
    """Мутирует factors IN PLACE — добавляет available/source_quality/
    freshness/precision к каждому фактору (не только используется для
    confidence ниже, но и видно наружу в API/UI)."""
    for key, f in factors.items():
        f["available"] = _is_available(f)
        f["source_quality"] = _SOURCE_QUALITY.get(key, 0.2)
        f["freshness"] = _FRESHNESS.get(key, "unknown")
        f["precision"] = _PRECISION.get(key, "unknown")


def _effective_source_quality(key: str, factors: dict) -> float:
    """source_quality ФАКТИЧЕСКИ используемого измерения этого фактора —
    задача 2026-08-16 ("Локальный OSM-слой", п.4 graceful degradation).
    Если фактор уже прошёл _annotate_factor_metadata() (и, может быть,
    ДОПОЛНИТЕЛЬНО уценён _apply_freshness_confidence_penalty() за
    устаревший city_poi) — берём f["source_quality"] как есть; иначе
    (тесты/вызовы без предварительной аннотации — прежнее поведение)
    статический дефолт схемы, как раньше было ЕДИНСТВЕННЫМ источником
    здесь."""
    f = factors.get(key)
    if f is not None and "source_quality" in f:
        return f["source_quality"]
    return _SOURCE_QUALITY.get(key, 0.2)


def _compute_confidence(factors: dict) -> int:
    """0-100, взвешено по source_quality (см. докстринг модуля) — ЗАМЕНЯЕТ
    старый плоский "доля посчитанных факторов" (каждый весил одинаково
    независимо от качества источника). Это ОБЩИЙ confidence на всю
    локацию — см. _group_confidence() ниже для confidence КАЖДОГО из
    пяти свойств отдельно (задача 2026-08-15 v2, коммит "Confidence").

    total_weight (знаменатель) СОЗНАТЕЛЬНО берётся по статическому
    _SOURCE_QUALITY (максимум качества "по схеме"), а не по фактическому
    — иначе устаревание city_poi обесценивало бы числитель и знаменатель
    ОДИНАКОВО и confidence не менялся бы вовсе (см. _effective_source_
    quality/_apply_freshness_confidence_penalty)."""
    total_weight = sum(_SOURCE_QUALITY.get(k, 0.2) for k in factors)
    if total_weight <= 0:
        return 0
    available_weight = sum(
        _effective_source_quality(k, factors) for k, f in factors.items() if _is_available(f))
    return round(100 * available_weight / total_weight)


def _group_confidence(group: str, factors: dict) -> int:
    """0-100 — confidence ОДНОГО свойства (не всей локации, см.
    _compute_confidence() выше) — задача 2026-08-15 v2, коммит
    "Confidence": "score X/100, confidence Y%" на КАЖДОЕ из пяти
    свойств, а не только один общий % на всю локацию. "73/31%" значит
    "из измеренного получается 73, но данных по этому свойству мало",
    НЕ "уверены, что оно хорошее".

    Та же логика source_quality, что и _compute_confidence(), но
    взвешена ТОЛЬКО по факторам ВНУТРИ этого свойства (_GROUPS[group]),
    не по всей схеме. `urban_quality` со СЕЙЧАС пустым _GROUPS[...]=()
    даёт confidence=0 СТРУКТУРНО, всегда (total_weight=0 -> return 0
    раньше деления на ноль) — Unknown ≠ average, не 50%/нейтрально, а
    честный ноль: "мы вообще не можем это измерить сейчас"."""
    keys = _GROUPS[group]
    total_weight = sum(_SOURCE_QUALITY.get(k, 0.2) for k in keys)  # знаменатель — см. _compute_confidence
    if total_weight <= 0:
        return 0
    available_weight = sum(
        _effective_source_quality(k, factors) for k in keys if k in factors and _is_available(factors[k]))
    return round(100 * available_weight / total_weight)


async def _apply_freshness_confidence_penalty(factors: dict) -> None:
    """Задача 2026-08-16 ("Локальный OSM-слой", п.4 "Graceful
    degradation") — мутирует factors IN PLACE, уценивая source_quality
    OSM-факторов (_OSM_LOCAL_FACTOR_KEYS) пропорционально возрасту
    city_poi (MIN(updated_at) среди _OSM_LOCAL_KINDS — см. докстринг
    bot/score_layers/osm.py::city_poi_freshness_days). НЕ блокирует
    скоринг ни при каких обстоятельствах — только снижает доверие:
    >14 дней ×0.8, >30 дней ×0.5 (не перемножаются — берётся более
    строгий порог). None (city_poi по этим kind ещё вообще не
    синхронизирована — другой кейс, не "устарела", а "никогда не была")
    -> ничего не трогаем, эти факторы и так идут по live Overpass-
    фолбэку внутри bot/score_layers/*.py, а не по этой метрике."""
    from bot.score_layers.osm import city_poi_freshness_days
    age_days = await city_poi_freshness_days(_OSM_LOCAL_KINDS)
    if age_days is None:
        return
    mult = 1.0
    if age_days > 30:
        mult = _STALE_CONFIDENCE_MULT_30D
    elif age_days > 14:
        mult = _STALE_CONFIDENCE_MULT_14D
    if mult == 1.0:
        return
    logger.warning("city_poi stale, fetched_at≈%.1f дней назад (source_quality ×%.1f для %s)",
                   age_days, mult, ", ".join(_OSM_LOCAL_FACTOR_KEYS))
    for key in _OSM_LOCAL_FACTOR_KEYS:
        f = factors.get(key)
        if f is not None and "source_quality" in f:
            f["source_quality"] = round(f["source_quality"] * mult, 3)


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

# Свежесть строки complex_walkability, при которой walking-дистанция
# считается актуальной (Фаза L3, задача 2026-08-15). Снапшот ежемесячный
# (krisha-complex-walkability.timer, 1-е число) — 45 дней покрывает один
# пропущенный прогон; старше — фолбэк на прямую линию.
_WALKABILITY_MAX_AGE_DAYS = 45


async def _walkability_row(complex_id: int | None, destination_type: str):
    """Свежая (< _WALKABILITY_MAX_AGE_DAYS) строка complex_walkability для
    ЖК/типа назначения (Фаза L3 walkability, миграция 075, писатель —
    complex_walkability_snapshot.py). None — нет complex_id (вызов по
    голым координатам), строки нет/устарела или сбой БД: вызывающий код
    фолбэкается на прямую линию (мягкая деградация, как везде здесь)."""
    if complex_id is None:
        return None
    from bot.db.pg import fetchrow
    try:
        return await fetchrow("""
            SELECT walking_distance_m, haversine_distance_m, barrier,
                   dest_name, dest_lat, dest_lon, no_route_reason
            FROM complex_walkability
            WHERE complex_id = $1 AND destination_type = $2
              AND computed_at > now() - ($3 || ' days')::interval
            ORDER BY computed_at DESC LIMIT 1
        """, complex_id, destination_type, str(_WALKABILITY_MAX_AGE_DAYS))
    except Exception as exc:
        logger.warning("complex_walkability lookup failed: %s", exc)
        return None


async def _school_type_rating_at(lat: float, lon: float) -> dict:
    """type/rating_2gis школы ПО КООРДИНАТАМ назначения из walking-строки
    (ближайшая пешком школа может отличаться от ближайшей по прямой —
    тип/рейтинг надо брать у той, до которой реально довёл маршрут;
    dest_*) пришёл из самой astana_schools, поэтому ближайшая к dest
    запись — это она и есть)."""
    from bot.db.pg import fetchrow
    try:
        row = await fetchrow("""
            SELECT type, rating_2gis FROM astana_schools
            WHERE lat IS NOT NULL AND lon IS NOT NULL
            ORDER BY (lat - $1)^2 + (lon - $2)^2 LIMIT 1
        """, lat, lon)
    except Exception as exc:
        logger.warning("astana_schools type lookup failed: %s", exc)
        row = None
    return {"type": (row["type"] or "").strip() if row else "",
            "rating_2gis": row["rating_2gis"] if row else None}


def _school_adj_from_distance(dist_m: float, school_type: str, rating,
                              walk_note: str) -> dict:
    """Общая градация school_access по расстоянию + бонусы/штрафы за
    рейтинг 2GIS и тип (выделено из _schools_factor при добавлении
    walking-ветки, Фаза L3 — пороги НЕ менялись, меняется только смысл
    dist_m: маршрут пешком вместо прямой, если есть complex_walkability).
    walk_note — пометка источника расстояния для reason ('пешком' /
    '(по прямой...)')."""
    if dist_m <= 300:
        base_adj = 3
    elif dist_m <= 500:
        base_adj = 2
    elif dist_m <= 1000:
        base_adj = 1
    else:
        return {"adj": 0, "reason": f"ближайшая школа дальше 1км ({dist_m:.0f}м{walk_note})"}

    adj = base_adj
    rating_note = ""
    if rating is not None:
        if rating >= 4.5:
            adj += 1
            rating_note = f", рейтинг 2GIS {rating} — отличный"
        elif rating < 3.5:
            adj -= 1
            rating_note = f", рейтинг 2GIS {rating} — низкий"

    if school_type.lower() in _SCHOOL_BONUS_TYPES:
        return {"adj": adj + 1, "reason": f"школа в {dist_m:.0f}м{walk_note} ({school_type}, углублённая программа){rating_note}"}
    return {"adj": adj, "reason": f"школа в {dist_m:.0f}м{walk_note} ({school_type or 'тип не указан'}){rating_note}"}


async def _schools_factor(lat: float, lon: float, complex_id: int | None = None) -> dict:
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

    **Фаза L3 walkability (задача 2026-08-15, миграция 075)**: при
    наличии `complex_id` расстояние берётся из свежей (<45 дней) строки
    `complex_walkability` — реальный пешеходный маршрут OSRM foot вместо
    прямой (complex_walkability_snapshot.py, ежемесячно). Ближайшая
    ПЕШКОМ школа может отличаться от ближайшей по прямой (река/трасса) —
    тип/рейтинг тогда добираются по координатам назначения из walking-
    строки, не по точке ЖК. Маршрут не построен (walking=NULL) →
    хаверсин из той же строки с честной пометкой. Строки нет/устарела/
    нет complex_id → прежняя SQL-аппроксимация ниже (мягкая деградация).
    Пороги 300/500/1000м НЕ менялись — меняется только смысл dist_m.
    """
    w = await _walkability_row(complex_id, "school")
    if w is not None:
        if w["dest_lat"] is not None:
            meta = await _school_type_rating_at(w["dest_lat"], w["dest_lon"])
        else:
            meta = {"type": "", "rating_2gis": None}
        if w["walking_distance_m"] is not None:
            note = " пешком"
            if w["barrier"]:
                note += (f" (по прямой {w['haversine_distance_m']:.0f}м — "
                         f"⚠️ вероятный барьер: река/трасса)")
            return _school_adj_from_distance(
                w["walking_distance_m"], meta["type"], meta["rating_2gis"], note)
        return _school_adj_from_distance(
            w["haversine_distance_m"], meta["type"], meta["rating_2gis"],
            " по прямой (маршрут не построен)")

    from bot.db.pg import fetchrow
    try:
        row = await fetchrow("""
            SELECT type, rating_2gis,
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
    return _school_adj_from_distance(
        dist_m, (row["type"] or "").strip(), row["rating_2gis"], "")


async def _kindergartens_factor(lat: float, lon: float, complex_id: int | None = None) -> dict:
    """Точный фактор по ближайшему садику (`astana_kindergartens`, 131
    строка на 2026-08-15). Только расстояние — без бонуса за тип: колонка
    `type` в этой таблице на 100% пустая (в отличие от `astana_schools`),
    бонусировать нечем. См. докстринг `_schools_factor()` выше про
    осознанное частичное двойное взвешивание с OSM-слоем schools, про
    ограничение свежести (нет скрипта-писателя) и про Фазу L3
    walkability — при наличии `complex_id` расстояние берётся из свежей
    строки `complex_walkability` (пешком, OSRM) вместо прямой, пороги
    300/500м не менялись."""
    w = await _walkability_row(complex_id, "kindergarten")
    if w is not None:
        if w["walking_distance_m"] is not None:
            dist_m = w["walking_distance_m"]
            note = " пешком"
            if w["barrier"]:
                note += (f" (по прямой {w['haversine_distance_m']:.0f}м — "
                         f"⚠️ вероятный барьер: река/трасса)")
        else:
            dist_m = w["haversine_distance_m"]
            note = " по прямой (маршрут не построен)"
        if dist_m <= 300:
            return {"adj": 2, "reason": f"садик в {dist_m:.0f}м{note}"}
        if dist_m <= 500:
            return {"adj": 1, "reason": f"садик в {dist_m:.0f}м{note}"}
        return {"adj": 0, "reason": f"ближайший садик дальше 500м ({dist_m:.0f}м{note})"}

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


async def _air_quality_factor(lat: float, lon: float) -> dict:
    """Индекс загрязнения воздуха (`air_stations` — ПНЗ Казгидромета,
    ecodata.kz, задача 2026-08-15 "воздух в location_score" — Task 3,
    начата после завершения Location Reliability Phase, как явно
    требовал заказчик). `index_value` = max(факт/ПДК м.р.) по
    загрязнителям (см. докстринг `pnz_collect.py`) — 1.0 означает РОВНО
    на пределе допустимой концентрации, не абстрактная шкала 1-10.

    **Проверено перед реализацией, не на веру**: на 2026-08-15 (295
    строк истории, 10 станций) `index_value` НИ РАЗУ не превысил 0.2 —
    воздух Астаны по этому индексу сейчас стабильно "в пределах нормы".
    Значит adj=0 будет ТИПИЧНЫМ результатом прямо сейчас на живых
    данных — это честное отражение действительности (пороги не
    занижены искусственно под текущие цифры, взяты из спеки заказчика
    как есть), фактор станет содержательным при реальном ухудшении
    (напр. отопительный сезон/смог) без единой правки кода.

    `air_stations` — TIME SERIES (295 строк на 10 станций, ~30 записей
    на станцию, копится почасовым таймером `krisha-air-stations.timer`)
    — берём САМОЕ СВЕЖЕЕ измерение КАЖДОЙ станции (DISTINCT ON), потом
    ищем ближайшую из них, а не наоборот (наивный ORDER BY расстояние
    LIMIT 1 по всей таблице рисковал бы вернуть старую запись ближней
    станции вместо её же свежей)."""
    from bot.db.pg import fetchrow
    try:
        row = await fetchrow("""
            SELECT station_name, index_value, index_pollutant,
                   (((lat - $1) * 111.0)^2 + ((lon - $2) * 111.0 * 0.63)^2) AS d2
            FROM (
                SELECT DISTINCT ON (station_name) station_name, lat, lon,
                       index_value, index_pollutant, fetched_at
                FROM air_stations
                ORDER BY station_name, fetched_at DESC
            ) latest
            ORDER BY d2 LIMIT 1
        """, lat, lon)
    except Exception as exc:
        logger.warning("air_stations lookup failed: %s", exc)
        row = None
    if not row:
        return {"adj": 0, "reason": "нет данных air_stations рядом"}

    import math
    dist_m = math.sqrt(row["d2"]) * 1000
    if dist_m > 5000:
        return {"adj": 0, "reason": "нет станции в радиусе"}

    index_value = row["index_value"]
    if index_value is None:
        return {"adj": 0, "reason": f"ближайшая станция {row['station_name']} без данных индекса"}
    index_value = float(index_value)

    if index_value < 1.0:
        adj = 0
    elif index_value < 2.0:
        adj = -1
    elif index_value < 5.0:
        adj = -2
    else:
        adj = -3

    reason = (f"Ближайшая станция: {row['station_name']} ({dist_m:.0f}м), "
              f"индекс {index_value} ({row['index_pollutant']})")
    return {"adj": adj, "reason": reason}


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
    complex_id: int | None = None,
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
    докстринг _building_age_factor() выше, почему).

    `complex_id` — Фаза L3 walkability (задача 2026-08-15, миграция 075):
    при наличии school_access/kindergarten_access берут расстояние из
    свежей строки complex_walkability (реальный маршрут пешком, OSRM)
    вместо прямой; None (вызов по голым координатам) — прежний путь."""
    if not lat or not lon:
        return None
    listing = {"lat": lat, "lon": lon}
    factors: dict[str, dict] = {}

    # Точные DB-факторы (не Overpass — общий pg pool, дешёво) идут ПЕРВЫМИ,
    # раньше OSM-слоёв ниже — их результат нужен, чтобы решить, в каком
    # режиме звать OSM-слой "schools" (см. ниже про double-counting).
    hex_factors, demolition_result, schools_result, kindergartens_result, air_quality_result = await asyncio.gather(
        _transport_hex_factors(lat, lon),
        _demolition_factor(lat, lon),
        _schools_factor(lat, lon, complex_id),
        _kindergartens_factor(lat, lon, complex_id),
        _air_quality_factor(lat, lon),
    )
    factors["lrt_access"] = {**hex_factors["lrt_access"], "label": "🚈 ЛРТ рядом"}
    factors["road_access"] = {**hex_factors["road_access"], "label": "🚗 Доступность на авто"}
    factors["route_connectivity"] = {**hex_factors["route_connectivity"], "label": "🔀 Маршрутная связность"}
    factors["demolition"] = {**demolition_result, "label": "🚧 Снос по соседству"}
    # air_quality — задача 2026-08-15 "воздух в location_score" (Task 3).
    factors["air_quality"] = {**air_quality_result, "label": "💨 Качество воздуха"}
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

    # available/source_quality/freshness/precision на каждый фактор +
    # confidence, взвешенный по source_quality — задача 2026-08-15,
    # "Location Reliability Phase", коммит "Confidence" (см. докстринг
    # модуля). Заменяет старый плоский "доля посчитанных факторов".
    _annotate_factor_metadata(factors)
    # Graceful degradation по свежести city_poi (задача 2026-08-16,
    # "Локальный OSM-слой", п.4) — ПОСЛЕ _annotate_factor_metadata (нужен
    # уже проставленный f["source_quality"], который она уценивает), ДО
    # _compute_confidence/_group_confidence (которые его читают).
    await _apply_freshness_confidence_penalty(factors)
    total = sum(f["adj"] for f in factors.values())
    confidence = _compute_confidence(factors)
    # group_scores/group_confidence — задача 2026-08-15 v2, коммит
    # "Confidence": пара "score X/100, confidence Y%" на КАЖДОЕ из пяти
    # latent-свойств (не только общий confidence всей локации выше).
    group_scores = {g: round(_group_pct(g, factors)) for g in _GROUPS}
    group_confidence = {g: _group_confidence(g, factors) for g in _GROUPS}

    return {
        "total": total, "factors": factors, "confidence": confidence,
        "group_scores": group_scores, "group_confidence": group_confidence,
    }
