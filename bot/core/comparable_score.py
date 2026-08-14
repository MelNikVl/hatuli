"""comparable_score v2 (Фаза B, задача 2026-08-14, docs/verdict_strategy.md
"Фаза B — comparable engine v2 (фокус на price_score)") — непрерывный
скор 0-1 сопоставимости ПАРЫ объявлений, вместо текущего бинарного
отбора (`deal_score.py`/`bargain.py`: объявление либо попадает в
`AREA_BAND_PCT`/`MIN_*`-полосу, либо нет, дальше все "прошедшие" равны).

**Это пункт 1 Фазы B — только ядро + тесты.** `AREA_BAND_PCT`/`MIN_BLDG`/
`MIN_HEX`/`MIN_RING` (`bot/core/hedonic_constants.py`) ОСТАЮТСЯ порогами
отсечения — не заменяются, `comparable_score` ранжирует ВНУТРИ уже
отсечённого множества. Интеграция в `deal_score.py` (weighted median
топ-N вместо плоской медианы) — Фаза B п.2, отдельная задача/коммит, этот
модуль её пока не делает и `deal_score.py` не трогает.

**Мотивация** (честный `as_of`-backtest, `docs/verdict_strategy.md`,
раздел «Результаты честного backtest (2026-08-14)»): `price_score` —
единственный компонент Deal Score с измеренным (3 независимых `t0`)
резервом роста, AUC честно ≈0.71-0.72. Рычаг — не больше факторов в
score_total (`quality`/`market`/`risk` в Фазе B не трогаются), а точнее
ПУЛ СРАВНЕНИЯ, из которого строится `P_expected`.

**Принцип "Unknown ≠ average"** (`docs/verdict_strategy.md` §3.1):
фактор, неизвестный хотя бы у одной стороны пары, ИСКЛЮЧАЕТСЯ из
взвешенной суммы (не штрафуется до 0, не подставляется дефолт/среднее),
вес перенормируется по фактически известным факторам — тот же приём,
что `quality`-компонент `deal_score.py` уже применяет к неизвестному
классу/году/рейтингу. Пара, где неизвестно вообще ВСЁ (`total_w == 0`) —
`comparable_score = 0.0`: честно "нет оснований считать похожими", не
"средняя похожесть".

**`as_of`** — параметр первого класса с самого начала (не пришит поверх
задним числом, как у `deal_score.py`/`apply_deal_scores()`, см. задачу
"as_of для score_total, минимальный план"): использует `is_active_as_of()`
(`bot/core/hedonic_constants.py`) — Python-твин `_activity_filter()` для
уже загруженных пар, та же троичная логика. `as_of=None` (прод-путь по
умолчанию) — поведение не завязано на активность вовсе, чистое сравнение
атрибутов (сам вызывающий код отвечает за то, что подал только активные
на сейчас кандидаты, как и раньше). `as_of=t0` — `listing_b`
(кандидат-аналог), не активный на `t0`, получает `comparable_score=0.0`
безусловно, независимо от похожести остальных атрибутов.
"""
from __future__ import annotations

import math
from datetime import datetime

from bot.core.geo import haversine_km
from bot.core.hedonic_constants import (
    AREA_BAND_PCT, _CLASS_SCORE, _class_key, _FINISH_QUALITY_SCORE, is_active_as_of,
)

# Веса факторов (сумма = 1.0). same_building/same_complex — НЕ
# эксклюзивная замена continuous-факторов (area/floor/...), а
# независимые слагаемые: один и тот же физический дом почти всегда даёт
# максимум по остальным факторам тоже — это согласие свидетельств, не
# двойной счёт одного и того же явления.
_WEIGHTS = {
    "same_building": 0.30,
    "same_complex": 0.15,
    "area": 0.20,
    "floor": 0.05,
    "year_built": 0.10,
    "housing_class": 0.10,
    "finish_level": 0.05,
    "distance": 0.05,
}
assert abs(sum(_WEIGHTS.values()) - 1.0) < 1e-9, "веса comparable_score должны суммироваться в 1.0"

_YEAR_DECAY_YEARS = 10.0   # похожесть года: 1.0 при разнице 0 лет, 0.0 при >=10
_DISTANCE_DECAY_M = 500.0  # похожесть по расстоянию: экспоненциальный спад, e^-1 на 500м


def _same_building(a: dict, b: dict) -> float | None:
    ha, hb = a.get("resolved_house_id"), b.get("resolved_house_id")
    if ha is None or hb is None:
        return None
    return 1.0 if ha == hb else 0.0


def _same_complex(a: dict, b: dict) -> float | None:
    # complex_id — РЕЗОЛВЛЕННЫЙ id ЖК (дом или зонтик), не сырой текст
    # complex_name — та же логика, что _listing_id_match/resolved_house_id
    # everywhere в проекте (terminal_extras.py) — резолюция это забота
    # вызывающего кода, эта функция чистая, в БД не ходит.
    ca, cb = a.get("complex_id"), b.get("complex_id")
    if ca is None or cb is None:
        return None
    return 1.0 if ca == cb else 0.0


def _area_similarity(area_a, area_b) -> float | None:
    if not area_a or not area_b or area_a <= 0:
        return None
    diff_pct = abs(area_a - area_b) / area_a
    return max(0.0, 1.0 - diff_pct / AREA_BAND_PCT)


def _floor_similarity(floor_a, floor_b) -> float | None:
    if floor_a is None or floor_b is None:
        return None
    return 1.0 / (1.0 + abs(floor_a - floor_b))


def _year_similarity(year_a, year_b) -> float | None:
    if not year_a or not year_b:
        return None
    return max(0.0, 1.0 - abs(year_a - year_b) / _YEAR_DECAY_YEARS)


def _housing_class_similarity(class_a, class_b) -> float | None:
    key_a, key_b = _class_key(class_a or ""), _class_key(class_b or "")
    if key_a is None or key_b is None:
        return None
    if key_a == key_b:
        return 1.0
    span = max(_CLASS_SCORE.values()) - min(_CLASS_SCORE.values())
    return max(0.0, 1.0 - abs(_CLASS_SCORE[key_a] - _CLASS_SCORE[key_b]) / span)


def _finish_level_similarity(finish_a, finish_b) -> float | None:
    score_a, score_b = _FINISH_QUALITY_SCORE.get(finish_a), _FINISH_QUALITY_SCORE.get(finish_b)
    if score_a is None or score_b is None:
        return None
    if score_a == score_b:
        return 1.0
    span = max(_FINISH_QUALITY_SCORE.values()) - min(_FINISH_QUALITY_SCORE.values())
    return max(0.0, 1.0 - abs(score_a - score_b) / span)


def _distance_similarity(lat_a, lon_a, lat_b, lon_b) -> float | None:
    if lat_a is None or lon_a is None or lat_b is None or lon_b is None:
        return None
    dist_m = haversine_km(float(lat_a), float(lon_a), float(lat_b), float(lon_b)) * 1000.0
    return math.exp(-dist_m / _DISTANCE_DECAY_M)


def compute_comparable_score(listing_a: dict, listing_b: dict,
                              as_of: datetime | None = None,
                              weights: dict | None = None) -> float:
    """Скор сопоставимости пары объявлений, 0.0-1.0 (выше — похожее).

    listing_a/listing_b: {lat, lon, area, floor, year_built,
        resolved_house_id, complex_id, housing_class, finish_level,
        first_seen, archived_at, is_active} — любое поле может
        отсутствовать/быть None, соответствующий фактор тогда честно
        исключается (Unknown ≠ average, см. докстринг модуля), а не
        трактуется как несовпадение.

    as_of: None (по умолчанию) — активность listing_b не проверяется
        (вызывающий сам отвечает за то, что подал только актуальные
        кандидаты). Дата — listing_b, не активный НА ЭТУ ДАТУ
        (`is_active_as_of()`, `bot/core/hedonic_constants.py`), получает
        0.0 безусловно — не аналог физически, не может быть похож.

    weights: переопределение весов факторов (см. `_WEIGHTS`) — доли,
        отсутствующие ключи берутся из дефолта; для калибровки/тестов,
        прод-вызов без параметра не меняется.
    """
    if as_of is not None:
        if not is_active_as_of(
            listing_b.get("first_seen"), listing_b.get("archived_at"),
            as_of, listing_b.get("is_active"),
        ):
            return 0.0

    w = {**_WEIGHTS, **(weights or {})}

    factors = {
        "same_building": _same_building(listing_a, listing_b),
        "same_complex": _same_complex(listing_a, listing_b),
        "area": _area_similarity(listing_a.get("area"), listing_b.get("area")),
        "floor": _floor_similarity(listing_a.get("floor"), listing_b.get("floor")),
        "year_built": _year_similarity(listing_a.get("year_built"), listing_b.get("year_built")),
        "housing_class": _housing_class_similarity(listing_a.get("housing_class"), listing_b.get("housing_class")),
        "finish_level": _finish_level_similarity(listing_a.get("finish_level"), listing_b.get("finish_level")),
        "distance": _distance_similarity(
            listing_a.get("lat"), listing_a.get("lon"), listing_b.get("lat"), listing_b.get("lon")),
    }

    total_w = 0.0
    total_score = 0.0
    for name, val in factors.items():
        if val is None:
            continue
        fw = w.get(name, 0.0)
        total_w += fw
        total_score += fw * val

    if total_w <= 0.0:
        return 0.0
    return total_score / total_w
