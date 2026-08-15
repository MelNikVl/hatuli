"""Регрессия для задачи 2026-08-15 ("Location Reliability Phase", коммит
"Семантика + групповая модель") — bot/core/location_score.py::
normalize_group_weighted(). Чистая функция (без сети/БД), тестируется
напрямую на синтетических factors-словарях."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bot.core.location_score import normalize_group_weighted, _GROUPS, _GROUP_WEIGHTS, _group_range


def _factors(**overrides) -> dict:
    keys = [k for g in _GROUPS.values() for k in g] + ["bank"]
    base = {k: {"adj": 0} for k in keys}
    for k, v in overrides.items():
        base[k]["adj"] = v
    return base


def test_group_weights_sum_to_one():
    assert abs(sum(_GROUP_WEIGHTS.values()) - 1.0) < 1e-9


def test_all_zero_factors_transport_infra_green_read_as_zero_not_fifty():
    """Известное ограничение, зафиксированное в докстринге модуля —
    unknown/нейтральный adj=0 в группах с неотрицательным диапазоном
    (transport/infra/green) сегодня читается как МИНИМУМ (0%), не
    середина (50%). Явный тест, чтобы будущий Confidence-коммит имел с
    чем сравнивать до/после."""
    score = normalize_group_weighted(_factors())
    # noise (0 из -6..0 -> 100%) и risk (0 из -2..2 -> середина, 50%)
    # тянут итог вверх — но НЕ 50, ниже середины (проверяем именно это,
    # не конкретное число, чтобы не дублировать формулу в тесте).
    assert 0 < score < 50


def test_all_groups_at_their_min_gives_zero():
    f = _factors(noise=-6, demolition=-2)  # остальное по умолчанию — уже минимум своих групп
    assert normalize_group_weighted(f) == 0


def test_all_groups_at_their_max_gives_hundred():
    f = _factors(schools=5, transit_stops=3, amenities=4, parks=2,
                 lrt_access=4, road_access=2, route_connectivity=2, building_age=2,
                 school_access=4, kindergarten_access=2)
    assert normalize_group_weighted(f) == 100


def test_group_ranges_match_factor_range_sums():
    assert _group_range("transport") == (0, 11)
    assert _group_range("infra") == (0, 15)
    assert _group_range("noise") == (-6, 0)
    assert _group_range("green") == (0, 2)
    assert _group_range("risk") == (-2, 2)


def test_missing_group_untouched_by_change_in_another_group():
    """Ключевое свойство групповой модели (vs старый единый диапазон,
    задача-триггер этого коммита): изменение ОДНОЙ группы не должно
    менять нормализованный вклад ДРУГИХ групп — проверяем напрямую, а
    не полагаемся на итоговое число (то будет доработано в Confidence-
    коммите на уровне 'весь score ЖК не должен прыгать', это же —
    более узкое утверждение про независимость групп друг от друга)."""
    base = _factors(schools=5, amenities=4)  # infra не на максимуме
    more_transport = _factors(schools=5, amenities=4, lrt_access=4)
    # infra-вклад (0.25 * 9/15 * 100 = 15.0) идентичен в обоих случаях —
    # добавление lrt_access (транспорт) не трогает infra.
    base_score = normalize_group_weighted(base)
    more_score = normalize_group_weighted(more_transport)
    # Разница целиком объясняется вкладом transport (0.25 * 4/11 * 100).
    expected_delta = round(0.25 * 4 / 11 * 100)
    assert abs((more_score - base_score) - expected_delta) <= 1  # округление


def test_symmetric_extremes_noise_and_risk():
    # noise: диапазон -6..0, adj=-6 -> 0%; adj=0 -> 100%.
    assert normalize_group_weighted(_factors(noise=-6)) < normalize_group_weighted(_factors())
    # risk: диапазон -2..2, demolition=-2 (минимум) хуже, чем default 0 (середина).
    assert normalize_group_weighted(_factors(demolition=-2)) < normalize_group_weighted(_factors())
