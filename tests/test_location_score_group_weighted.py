"""Регрессия для задачи 2026-08-15 ("Location Reliability Phase") —
bot/core/location_score.py::normalize_group_weighted() + _is_available()/
_group_range_available()/_compute_confidence()/_annotate_factor_metadata()
(коммиты "Семантика + групповая модель", "двойные школы + building_age",
"Confidence"). Чистые функции (без сети/БД), тестируются напрямую на
синтетических factors-словарях."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bot.core.location_score import (
    normalize_group_weighted, _GROUPS, _GROUP_WEIGHTS, _group_range, _FACTOR_RANGES,
    _is_available, _group_range_available, _compute_confidence, _annotate_factor_metadata,
)


def _factors(available=(), **overrides) -> dict:
    """`available` — ключи, которым выставляется reason БЕЗ "нет данных"
    (т.е. реально измеренные) — ПО УМОЛЧАНИЮ ВСЕ факторы "нет данных"
    (реалистичный дефолт после задачи "Confidence": ничего не измерено,
    пока явно не указано в `available`). `overrides` — adj для конкретных
    ключей, НЕЗАВИСИМО от `available` (можно задать adj фактору, который
    всё равно останется "нет данных" — редкий, но валидный кейс: слой
    вернул 0 c reason "нет данных", это ровно дефолт ниже)."""
    keys = [k for g in _GROUPS.values() for k in g] + ["bank"]
    base = {k: {"adj": 0, "reason": "нет данных"} for k in keys}
    for k in available:
        base[k]["reason"] = "измерено (тест)"
    for k, v in overrides.items():
        base[k]["adj"] = v
    return base


def test_group_weights_sum_to_one():
    assert abs(sum(_GROUP_WEIGHTS.values()) - 1.0) < 1e-9


def test_building_age_not_in_any_group():
    """Задача "двойные школы + building_age" (2026-08-15) — возраст
    здания это качество ЗДАНИЯ, не локации, убран из _GROUPS вовсе (сама
    _building_age_factor() сохранена в коде, просто не вызывается из
    compute_complex_location_score())."""
    all_keys = {k for keys in _GROUPS.values() for k in keys}
    assert "building_age" not in all_keys
    assert "building_age" not in _FACTOR_RANGES


# ── _is_available ────────────────────────────────────────────────────────

def test_is_available_detects_no_data_and_error():
    assert _is_available({"reason": "нет данных"}) is False
    assert _is_available({"reason": "нет данных astana_schools рядом"}) is False
    assert _is_available({"reason": "ошибка слоя: timeout"}) is False
    assert _is_available({"reason": "школа в 300м"}) is True
    assert _is_available({}) is True  # нет reason вовсе -> осторожный дефолт "доступен"


# ── normalize_group_weighted — availability закрывает известные
#    ограничения предыдущих коммитов (см. докстринг модуля) ──────────────

def test_all_unavailable_gives_exactly_fifty():
    """ЗАКРЫВАЕТ баг, задокументированный в предыдущих коммитах фазы:
    раньше unknown-фактор в группах с неотрицательным диапазоном (transport/
    infra/green) читался как МИНИМУМ (0%), не середина. Теперь — группа
    БЕЗ единого доступного фактора вносит РОВНО 50%, и так для всех пяти
    групп -> взвешенный итог тоже РОВНО 50, не "где-то между 0 и 50"."""
    f = _factors()  # ничего не available
    assert normalize_group_weighted(f) == 50


def test_all_groups_at_their_min_gives_zero():
    f = _factors(available=("noise", "demolition", "route_connectivity", "schools", "parks"),
                 noise=-6, demolition=-2)
    # route_connectivity/schools/parks остаются на дефолтном adj=0, что
    # ровно их СОБСТВЕННЫЙ минимум (диапазоны 0..2 у всех трёх).
    assert normalize_group_weighted(f) == 0


def test_all_groups_at_their_max_gives_hundred():
    f = _factors(available=("noise", "demolition", "route_connectivity", "schools", "parks"),
                 route_connectivity=2, schools=2, parks=2)
    # noise/demolition остаются на дефолтном adj=0 — их СОБСТВЕННЫЙ
    # максимум (диапазоны -6..0 и -2..0).
    assert normalize_group_weighted(f) == 100


def test_group_ranges_match_factor_range_sums():
    """_group_range() — СТАТИЧЕСКИЙ теоретический диапазон (для
    документации/отображения), normalize_group_weighted() его больше не
    использует напрямую (см. _group_range_available() ниже)."""
    assert _group_range("transport") == (0, 11)
    # infra: schools(0,2)+amenities(0,4)+school_access(0,4)+
    # kindergarten_access(0,2) — schools сжат с 0..5 до 0..2 (задача
    # "двойные школы", 2026-08-15).
    assert _group_range("infra") == (0, 12)
    assert _group_range("noise") == (-6, 0)
    assert _group_range("green") == (0, 2)
    # risk: только demolition — building_age убран (та же задача).
    assert _group_range("risk") == (-2, 0)


def test_group_range_available_none_when_nothing_measured():
    assert _group_range_available("transport", _factors()) is None


def test_group_range_available_only_counts_available_keys():
    f = _factors(available=("schools",), schools=1)
    assert _group_range_available("infra", f) == (0, 2)  # только schools, не остальные 3 infra-фактора


def test_missing_group_untouched_by_change_in_another_group():
    """Ключевое свойство групповой модели (vs старый единый диапазон,
    задача-триггер коммита "Семантика + групповая модель"): изменение
    ОДНОЙ группы не должно менять нормализованный вклад ДРУГИХ групп."""
    base = _factors(available=("schools", "amenities"), schools=2, amenities=4)
    more_transport = _factors(available=("schools", "amenities", "lrt_access"),
                               schools=2, amenities=4, lrt_access=4)
    base_score = normalize_group_weighted(base)
    more_score = normalize_group_weighted(more_transport)
    # infra идентична в обоих случаях (schools=2/2 max + amenities=4/4 max
    # -> 100% в обоих) — вся разница из transport: 50% (ничего не
    # измерено) -> 100% (lrt_access=4 из своего же диапазона 0..4).
    expected_delta = round(_GROUP_WEIGHTS["transport"] * 50)
    assert abs((more_score - base_score) - expected_delta) <= 1  # округление


def test_symmetric_extremes_noise_and_risk():
    lo_noise = _factors(available=("noise",), noise=-6)
    hi_noise = _factors(available=("noise",), noise=0)
    assert normalize_group_weighted(lo_noise) < normalize_group_weighted(hi_noise)
    lo_risk = _factors(available=("demolition",), demolition=-2)
    hi_risk = _factors(available=("demolition",), demolition=0)
    assert normalize_group_weighted(lo_risk) < normalize_group_weighted(hi_risk)


def test_new_factor_neutral_for_existing_complex_does_not_move_score():
    """Прямая демонстрация свойства, которое проверяет stability-тест
    (следующий/последний коммит фазы): "ЖК", у которого appeared новый
    гипотетический фактор внутри infra остался НЕ измерен (unavailable) —
    его score НЕ ДОЛЖЕН измениться по сравнению с состоянием до появления
    этого фактора (ключа просто нет в factors вовсе)."""
    before = _factors(available=("schools",), schools=1)
    after_with_unmeasured_new_key = dict(before)
    after_with_unmeasured_new_key["kindergarten_access"] = {"adj": 0, "reason": "нет данных"}
    assert normalize_group_weighted(before) == normalize_group_weighted(after_with_unmeasured_new_key)


# ── _compute_confidence ──────────────────────────────────────────────────

def test_compute_confidence_weighted_by_source_quality():
    """Один доступный фактор из точного реестра (school_access,
    source_quality=0.8) даёт БОЛЬШЕ confidence, чем один доступный
    фактор из OSM (schools, 0.6) при равном общем числе факторов схемы —
    раньше (плоский счётчик) оба давали одинаковый %."""
    f_precise = _factors(available=("school_access",))
    f_osm = _factors(available=("schools",))
    assert _compute_confidence(f_precise) > _compute_confidence(f_osm)


def test_compute_confidence_nothing_available_is_zero():
    assert _compute_confidence(_factors()) == 0


def test_compute_confidence_everything_available_is_hundred():
    all_keys = [k for g in _GROUPS.values() for k in g] + ["bank"]
    f = _factors(available=tuple(all_keys))
    assert _compute_confidence(f) == 100


# ── _annotate_factor_metadata ────────────────────────────────────────────

def test_annotate_factor_metadata_sets_all_four_dimensions():
    f = _factors(available=("noise",), noise=-3)
    _annotate_factor_metadata(f)
    assert f["noise"]["available"] is True
    assert 0 < f["noise"]["source_quality"] <= 1
    assert f["noise"]["freshness"] in ("live", "periodic", "manual")
    assert f["noise"]["precision"] in ("exact", "presence", "heuristic")
    assert f["schools"]["available"] is False  # не в available -> "нет данных"
