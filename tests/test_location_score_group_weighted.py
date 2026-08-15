"""Регрессия для задачи 2026-08-15 ("Location Reliability Phase", коммит
"Семантика + групповая модель", коммит "двойные школы + building_age") —
bot/core/location_score.py::normalize_group_weighted(). Чистая функция
(без сети/БД), тестируется напрямую на синтетических factors-словарях."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bot.core.location_score import normalize_group_weighted, _GROUPS, _GROUP_WEIGHTS, _group_range, _FACTOR_RANGES


def _factors(**overrides) -> dict:
    keys = [k for g in _GROUPS.values() for k in g] + ["bank"]
    base = {k: {"adj": 0} for k in keys}
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


def test_all_zero_factors_transport_infra_green_read_as_zero_not_fifty():
    """Известное ограничение, зафиксированное в докстринге модуля —
    unknown/нейтральный adj=0 в группах с неотрицательным диапазоном
    (transport/infra/green) сегодня читается как МИНИМУМ (0%), не
    середина (50%); noise/risk (оба -X..0) наоборот читают 0 как МАКСИМУМ
    (100%). Явный тест, чтобы будущий Confidence-коммит имел с чем
    сравнивать до/после."""
    score = normalize_group_weighted(_factors())
    # transport(0%)+infra(0%)+green(0%) тянут вниз, noise(100%)+risk(100%)
    # тянут вверх — итог где-то посередине, НЕ 0 и НЕ 100.
    assert 0 < score < 100


def test_all_groups_at_their_min_gives_zero():
    f = _factors(noise=-6, demolition=-2)  # остальное по умолчанию — уже минимум своих групп
    assert normalize_group_weighted(f) == 0


def test_all_groups_at_their_max_gives_hundred():
    # schools=2 (не 5, задача "двойные школы" — OSM-часть школ/садиков
    # переехала в school_access/kindergarten_access, "schools" в
    # location_score теперь фактически "вуз рядом" 0..2), demolition=0
    # (default) — уже максимум risk-группы (диапазон -2..0), building_age
    # больше не участвует вовсе.
    f = _factors(schools=2, transit_stops=3, amenities=4, parks=2,
                 lrt_access=4, road_access=2, route_connectivity=2,
                 school_access=4, kindergarten_access=2)
    assert normalize_group_weighted(f) == 100


def test_group_ranges_match_factor_range_sums():
    assert _group_range("transport") == (0, 11)
    # infra: schools(0,2)+amenities(0,4)+school_access(0,4)+
    # kindergarten_access(0,2) — schools сжат с 0..5 до 0..2 (задача
    # "двойные школы", 2026-08-15).
    assert _group_range("infra") == (0, 12)
    assert _group_range("noise") == (-6, 0)
    assert _group_range("green") == (0, 2)
    # risk: только demolition — building_age убран (та же задача).
    assert _group_range("risk") == (-2, 0)


def test_missing_group_untouched_by_change_in_another_group():
    """Ключевое свойство групповой модели (vs старый единый диапазон,
    задача-триггер коммита "Семантика + групповая модель"): изменение
    ОДНОЙ группы не должно менять нормализованный вклад ДРУГИХ групп —
    проверяем напрямую, а не полагаемся на итоговое число (то будет
    доработано в Confidence-коммите на уровне 'весь score ЖК не должен
    прыгать', это же — более узкое утверждение про независимость групп
    друг от друга)."""
    base = _factors(schools=2, amenities=4)  # infra не на максимуме
    more_transport = _factors(schools=2, amenities=4, lrt_access=4)
    base_score = normalize_group_weighted(base)
    more_score = normalize_group_weighted(more_transport)
    # Разница целиком объясняется вкладом transport — считаем формулой,
    # не хардкодим числа диапазона (устойчиво к будущим пересчётам).
    lo, hi = _group_range("transport")
    expected_delta = round(_GROUP_WEIGHTS["transport"] * (4 - 0) / (hi - lo) * 100)
    assert abs((more_score - base_score) - expected_delta) <= 1  # округление


def test_symmetric_extremes_noise_and_risk():
    # noise: диапазон -6..0, adj=-6 -> 0%; adj=0 (default) -> 100%.
    assert normalize_group_weighted(_factors(noise=-6)) < normalize_group_weighted(_factors())
    # risk: диапазон -2..0 (только demolition с 2026-08-15), demolition=-2
    # (минимум, 0%) хуже, чем default 0 (максимум, 100%).
    assert normalize_group_weighted(_factors(demolition=-2)) < normalize_group_weighted(_factors())
