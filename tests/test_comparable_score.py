"""Регрессия для Фазы B, п.1 вердикт-стратегии (docs/verdict_strategy.md,
задача 2026-08-14): bot/core/comparable_score.compute_comparable_score()
— непрерывный скор 0-1 сопоставимости пары объявлений. Чистая функция,
без БД."""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from bot.core.comparable_score import compute_comparable_score, _WEIGHTS


def _listing(**overrides):
    base = {
        "lat": 51.10, "lon": 71.40, "area": 60.0, "floor": 5, "year_built": 2020,
        "resolved_house_id": 1, "complex_id": 1,
        "housing_class": "комфорт", "finish_level": "finished",
        "first_seen": datetime(2026, 7, 1, tzinfo=timezone.utc), "archived_at": None,
        "is_active": True,
    }
    base.update(overrides)
    return base


def test_weights_sum_to_one():
    assert abs(sum(_WEIGHTS.values()) - 1.0) < 1e-9


def test_identical_pair_scores_one():
    a = _listing()
    b = _listing()
    assert compute_comparable_score(a, b) == pytest.approx(1.0)


def test_all_unknown_scores_zero_not_average():
    # Unknown ≠ average (docs/verdict_strategy.md §3.1) — пара, где
    # неизвестно ВСЁ, честно 0.0, не "средняя похожесть" 0.5.
    assert compute_comparable_score({}, {}) == 0.0


class TestSameBuilding:
    def test_match(self):
        a = _listing(resolved_house_id=42)
        b = _listing(resolved_house_id=42, complex_id=None)
        score = compute_comparable_score(a, b)
        assert score > 0.5

    def test_mismatch_lowers_score(self):
        a = _listing(resolved_house_id=1)
        b = _listing(resolved_house_id=2)
        same = compute_comparable_score(a, _listing(resolved_house_id=1))
        diff = compute_comparable_score(a, b)
        assert diff < same

    def test_unknown_excluded_not_penalized(self):
        # Одна сторона без resolved_house_id -> фактор исключён, не 0.
        a = _listing(resolved_house_id=None)
        b = _listing()
        score_with_unknown = compute_comparable_score(a, b)
        # Тот же b, но a с известным (не совпадающим) house_id -> должно
        # быть НИЖЕ (там явное несовпадение = 0.0 в факторе), чем при
        # неизвестности (фактор просто выпал).
        a_mismatch = _listing(resolved_house_id=999)
        score_mismatch = compute_comparable_score(a_mismatch, b)
        assert score_with_unknown > score_mismatch


class TestSameComplex:
    def test_match_without_same_building(self):
        a = _listing(resolved_house_id=1, complex_id=7)
        b = _listing(resolved_house_id=2, complex_id=7)  # другой дом, тот же ЖК
        score = compute_comparable_score(a, b)
        strangers = compute_comparable_score(a, _listing(resolved_house_id=2, complex_id=999))
        assert score > strangers


class TestAreaSimilarity:
    def test_same_area_full_score_on_that_factor(self):
        a = _listing(area=60.0)
        b = _listing(area=60.0)
        assert compute_comparable_score(a, b) == pytest.approx(1.0)

    def test_area_within_band_partial(self):
        a = _listing(area=60.0)
        b = _listing(area=65.0)  # +8.3%, внутри AREA_BAND_PCT=15%
        score = compute_comparable_score(a, b)
        assert 0.0 < score < 1.0

    def test_area_far_beyond_band_zero_contribution(self):
        a = _listing(area=60.0)
        near = _listing(area=63.0)
        far = _listing(area=200.0)  # далеко за полосой
        assert compute_comparable_score(a, far) < compute_comparable_score(a, near)

    def test_missing_area_excluded(self):
        a = _listing(area=None)
        b = _listing(area=60.0)
        # Не должно упасть и не должно быть 0 — остальные факторы совпадают.
        score = compute_comparable_score(a, b)
        assert score > 0.5


class TestFloorSimilarity:
    def test_same_floor(self):
        a, b = _listing(floor=5), _listing(floor=5)
        assert compute_comparable_score(a, b) == pytest.approx(1.0)

    def test_adjacent_floor_high_but_not_max(self):
        a, b = _listing(floor=5), _listing(floor=6)
        score = compute_comparable_score(a, b)
        assert 0.9 < score < 1.0

    def test_far_floor_lower(self):
        a = _listing(floor=1)
        near = _listing(floor=2)
        far = _listing(floor=20)
        assert compute_comparable_score(a, far) < compute_comparable_score(a, near)


class TestYearBuiltSimilarity:
    def test_same_year(self):
        a, b = _listing(year_built=2020), _listing(year_built=2020)
        assert compute_comparable_score(a, b) == pytest.approx(1.0)

    def test_far_year_lower(self):
        a = _listing(year_built=2020)
        near = _listing(year_built=2021)
        far = _listing(year_built=1990)
        assert compute_comparable_score(a, far) < compute_comparable_score(a, near)

    def test_unknown_year_excluded_not_zero(self):
        a = _listing(year_built=None)
        b = _listing(year_built=2020)
        score = compute_comparable_score(a, b)
        assert score > 0.5


class TestHousingClassSimilarity:
    def test_same_class(self):
        a = _listing(housing_class="комфорт")
        b = _listing(housing_class="комфорт")
        assert compute_comparable_score(a, b) == pytest.approx(1.0)

    def test_ordinal_distance_elite_vs_business_closer_than_elite_vs_econom(self):
        elite = _listing(housing_class="элит")
        business = _listing(housing_class="бизнес")
        econom = _listing(housing_class="эконом")
        s_close = compute_comparable_score(elite, business)
        s_far = compute_comparable_score(elite, econom)
        assert s_close > s_far

    def test_unknown_class_text_excluded(self):
        a = _listing(housing_class=None)
        b = _listing(housing_class="комфорт")
        score = compute_comparable_score(a, b)
        assert score > 0.5

    def test_class_normalization_by_substring(self):
        # "элит-класс" должен нормализоваться к "элит" (как в _class_key).
        a = _listing(housing_class="элит-класс")
        b = _listing(housing_class="элит")
        assert compute_comparable_score(a, b) == pytest.approx(1.0)


class TestFinishLevelSimilarity:
    def test_same_finish(self):
        a, b = _listing(finish_level="designer"), _listing(finish_level="designer")
        assert compute_comparable_score(a, b) == pytest.approx(1.0)

    def test_rough_vs_designer_far(self):
        a = _listing(finish_level="rough")
        near = _listing(finish_level="needs_repair")
        far = _listing(finish_level="designer")
        assert compute_comparable_score(a, far) < compute_comparable_score(a, near)

    def test_unknown_finish_excluded(self):
        a = _listing(finish_level=None)
        b = _listing(finish_level="designer")
        score = compute_comparable_score(a, b)
        assert score > 0.5

    def test_unrecognized_finish_code_excluded_gracefully(self):
        a = _listing(finish_level="some_future_code")
        b = _listing(finish_level="designer")
        # Не должно упасть — неизвестный код просто не находит score в
        # _FINISH_QUALITY_SCORE, фактор исключается.
        score = compute_comparable_score(a, b)
        assert 0.0 <= score <= 1.0


class TestDistanceSimilarity:
    def test_same_coords(self):
        a, b = _listing(lat=51.10, lon=71.40), _listing(lat=51.10, lon=71.40)
        assert compute_comparable_score(a, b) == pytest.approx(1.0)

    def test_far_coords_lower(self):
        a = _listing(lat=51.10, lon=71.40)
        near = _listing(lat=51.1005, lon=71.4005)
        far = _listing(lat=51.30, lon=71.60)
        assert compute_comparable_score(a, far) < compute_comparable_score(a, near)

    def test_missing_coords_excluded(self):
        a = _listing(lat=None, lon=None)
        b = _listing()
        score = compute_comparable_score(a, b)
        assert score > 0.5


class TestAsOf:
    def test_as_of_none_ignores_activity(self):
        # as_of не передан — активность listing_b не проверяется вовсе,
        # даже если он формально уже архивирован в переданных полях.
        a = _listing()
        archived_b = _listing(is_active=False, archived_at=datetime(2026, 7, 5, tzinfo=timezone.utc))
        score = compute_comparable_score(a, archived_b)
        assert score == pytest.approx(1.0)

    def test_as_of_excludes_listing_not_active_at_t0(self):
        t0 = datetime(2026, 7, 10, tzinfo=timezone.utc)
        a = _listing()
        # b появился ПОСЛЕ t0 — не мог быть аналогом на t0.
        b_future = _listing(first_seen=t0 + timedelta(days=5))
        assert compute_comparable_score(a, b_future, as_of=t0) == 0.0

    def test_as_of_excludes_listing_archived_before_t0(self):
        t0 = datetime(2026, 7, 10, tzinfo=timezone.utc)
        a = _listing()
        b_archived = _listing(
            first_seen=t0 - timedelta(days=30),
            archived_at=t0 - timedelta(days=5),
        )
        assert compute_comparable_score(a, b_archived, as_of=t0) == 0.0

    def test_as_of_includes_listing_active_at_t0_even_if_archived_now(self):
        t0 = datetime(2026, 7, 10, tzinfo=timezone.utc)
        a = _listing()
        # Архивирован СЕЙЧАС, но ПОСЛЕ t0 -> на t0 был активен.
        b = _listing(
            first_seen=t0 - timedelta(days=30),
            archived_at=t0 + timedelta(days=20),
            is_active=False,
        )
        score = compute_comparable_score(a, b, as_of=t0)
        assert score == pytest.approx(1.0)

    def test_as_of_zero_regardless_of_similarity(self):
        # Даже полностью идентичная пара -> 0.0, если b не активен на t0.
        t0 = datetime(2026, 7, 10, tzinfo=timezone.utc)
        a = _listing()
        b = _listing(first_seen=t0 + timedelta(days=1))
        assert compute_comparable_score(a, b, as_of=t0) == 0.0


class TestWeightsOverride:
    def test_custom_weights_change_result(self):
        a = _listing(area=60.0, floor=1)
        b = _listing(area=90.0, floor=1)  # area далеко, floor совпадает
        default_score = compute_comparable_score(a, b)
        area_heavy = compute_comparable_score(a, b, weights={"area": 0.9, "floor": 0.02, "same_building": 0.02,
                                                               "same_complex": 0.02, "year_built": 0.02,
                                                               "housing_class": 0.01, "finish_level": 0.005,
                                                               "distance": 0.005})
        assert area_heavy < default_score  # больше веса на плохо совпадающий фактор -> ниже скор

    def test_prod_call_without_weights_unaffected(self):
        # Прод-вызов без weights= не меняется по сравнению с дефолтом.
        a, b = _listing(), _listing(area=70.0)
        assert compute_comparable_score(a, b) == compute_comparable_score(a, b, weights=None)


def test_score_always_in_unit_interval():
    import random
    rng = random.Random(20260814)
    classes = ["элит", "бизнес", "комфорт", "эконом", None]
    finishes = ["rough", "prefinish", "needs_repair", "finished", "renovated", "furnished", "designer", None]
    for _ in range(200):
        a = _listing(
            area=rng.uniform(20, 200), floor=rng.randint(1, 25), year_built=rng.choice([None, rng.randint(1960, 2026)]),
            resolved_house_id=rng.choice([None, rng.randint(1, 5)]),
            complex_id=rng.choice([None, rng.randint(1, 5)]),
            housing_class=rng.choice(classes), finish_level=rng.choice(finishes),
            lat=rng.uniform(51.0, 51.3), lon=rng.uniform(71.3, 71.6),
        )
        b = _listing(
            area=rng.uniform(20, 200), floor=rng.randint(1, 25), year_built=rng.choice([None, rng.randint(1960, 2026)]),
            resolved_house_id=rng.choice([None, rng.randint(1, 5)]),
            complex_id=rng.choice([None, rng.randint(1, 5)]),
            housing_class=rng.choice(classes), finish_level=rng.choice(finishes),
            lat=rng.uniform(51.0, 51.3), lon=rng.uniform(71.3, 71.6),
        )
        score = compute_comparable_score(a, b)
        assert 0.0 <= score <= 1.0
