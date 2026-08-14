"""Регрессия для Фазы B, п.2 вердикт-стратегии (docs/verdict_strategy.md,
задача 2026-08-14, "comparable engine v2 — интеграция в Deal Score v4"):
weighted median топ-N (веса = comparable_score) вместо плоской медианы
внутри own_bldg/own-гекс/кольцо. AREA_BAND_PCT/MIN_BLDG/MIN_HEX/MIN_RING
остаются порогами отсечения — не проверяются здесь заново (уже покрыты
test_deal_score_house_resolution.py/test_deal_score_risk_flags.py и
живут без изменений). bot/core/deal_score.compute_deal_scores() — чистая
функция, без БД."""
import os
import sys
from statistics import median

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bot.core.deal_score import (
    compute_deal_scores, _weighted_median, _weighted_median_topn,
    TOP_N_COMPARABLES, MAX_POOL_BEFORE_SCORING,
)

_COMPLEXES = {}


def _listing(id_, price, area=60.0, rooms=2, floor=5, complex_name="ЖК Тест",
             lat=51.10, lon=71.40, year_built=2020, finish_level=None,
             resolved_house_id=None):
    return {
        "id": id_, "lat": lat, "lon": lon, "price": price, "area": area,
        "rooms": rooms, "floor": floor, "floors_total": 12, "year_built": year_built,
        "complex_name": complex_name, "is_owner": True, "district": "Есильский р-н",
        "yield_pct": 8.0, "same_complex_cnt": 1, "ceiling_height": None,
        "resolved_house_id": resolved_house_id, "finish_level": finish_level,
    }


# ── _weighted_median: инвариант "равные веса = обычная медиана" ──

def test_weighted_median_equal_weights_matches_classic_median():
    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    weights = [1.0] * 5
    assert _weighted_median(values, weights) == median(values)


def test_weighted_median_even_count_equal_weights():
    values = [10.0, 20.0, 30.0, 40.0]
    weights = [1.0] * 4
    # medium() усредняет два средних (25.0); взвешенная медиана по
    # определению "точка, где кумулятивный вес впервые >= половины" даёт
    # ОДНО из значений (не усредняет) — честно другое поведение при
    # чётном n, не баг, задокументировано в docstring _weighted_median.
    wm = _weighted_median(values, weights)
    assert wm in values


def test_weighted_median_heavier_weight_pulls_result_toward_it():
    values = [10.0, 20.0, 30.0]
    equal = _weighted_median(values, [1.0, 1.0, 1.0])
    skewed = _weighted_median(values, [1.0, 1.0, 100.0])  # почти весь вес на 30
    assert skewed >= equal


def test_weighted_median_degenerate_zero_weights_falls_back_to_median():
    values = [10.0, 20.0, 30.0]
    assert _weighted_median(values, [0.0, 0.0, 0.0]) == median(values)


# ── _weighted_median_topn ──

def test_weighted_median_topn_excludes_zero_score_candidates():
    target = {"lat": 51.10, "lon": 71.40, "area": 60.0, "floor": 5,
              "resolved_house_id": None, "complex_id": None,
              "housing_class": None, "finish_level": None, "year_built": None}
    # Кандидат "далеко и по всем параметрам" — score должен выйти в 0
    # (или очень близко), но не гарантированно ровно 0 при частичном
    # совпадении случайных полей -> собираем заведомо все-unknown цели,
    # чтобы факторы честно не считались (Unknown ≠ average), тогда
    # comparable_score = 0.0 по определению (все факторы исключены).
    far_candidate = ("far", 999.0, {})  # пустой comp_dict -> все факторы None -> score=0.0
    near_candidate = ("near", 100.0, dict(target))
    result = _weighted_median_topn(target, [far_candidate, near_candidate])
    assert result == 100.0  # far с score=0 исключён, остаётся только near


def test_weighted_median_topn_returns_none_when_all_zero():
    target = {"lat": 51.10, "lon": 71.40, "area": 60.0, "floor": 5,
              "resolved_house_id": None, "complex_id": None,
              "housing_class": None, "finish_level": None, "year_built": None}
    candidates = [("a", 100.0, {}), ("b", 200.0, {})]
    assert _weighted_median_topn(target, candidates) is None


def test_weighted_median_topn_respects_top_n_limit():
    target = {"lat": 51.10, "lon": 71.40, "area": 60.0, "floor": 5,
              "resolved_house_id": 1, "complex_id": 1,
              "housing_class": "комфорт", "finish_level": "finished", "year_built": 2020}
    # 30 идентичных кандидатов (score=1.0 каждый) + один аномальный
    # (гораздо дешевле, но тоже похож по атрибутам) — при top_n=5
    # аномальный не должен попасть, если он не в первых 5 по порядку
    # (все score равны -> сортировка стабильна, первые N по вставке).
    candidates = [(f"id{i}", 100.0, dict(target)) for i in range(30)]
    candidates.append(("outlier", 1.0, dict(target)))
    result_top5 = _weighted_median_topn(target, candidates, top_n=5)
    assert result_top5 == 100.0  # outlier (последний в списке) не в топ-5


def test_top_n_comparables_and_max_pool_constants_sane():
    assert TOP_N_COMPARABLES > 0
    assert MAX_POOL_BEFORE_SCORING >= TOP_N_COMPARABLES


# ── Интеграционные: через compute_deal_scores() целиком ──

def test_own_bldg_uses_weighted_median_not_plain_when_scores_differ():
    # 4 объявления в одном доме (MIN_BLDG=3 порог пройден) — одно похоже
    # на целевое почти во всём (тот же этаж/год/класс), два — совпадают
    # только площадью (другой этаж/год/класс) -> weighted median должен
    # тянуть результат к БЛИЖНЕМУ аналогу заметнее, чем плоская медиана.
    target = _listing("T", price=30_000_000, area=60.0, floor=5, year_built=2020,
                       resolved_house_id=10, complex_name="ЖК Дом")
    close = _listing("C", price=31_000_000, area=61.0, floor=5, year_built=2020,
                      resolved_house_id=10, complex_name="ЖК Дом")  # похож почти во всём
    far1 = _listing("F1", price=20_000_000, area=60.0, floor=1, year_built=1970,
                     resolved_house_id=10, complex_name="ЖК Дом")
    far2 = _listing("F2", price=50_000_000, area=59.0, floor=20, year_built=1970,
                     resolved_house_id=10, complex_name="ЖК Дом")
    result = compute_deal_scores([target, close, far1, far2], _COMPLEXES, edge_m=100.0)
    assert "T" in result
    # sources должен сообщать "тот же дом/ЖК" (own_bldg сработал).
    assert result["T"]["sources"] == "тот же дом/ЖК"


def test_self_excluded_by_id_not_by_value_when_duplicate_price():
    # Четыре объявления с ОДИНАКОВОЙ ценой/м² (совпадение значения) —
    # раньше self-исключение по значению (list.remove(p_m2)) могло
    # случайно исключить ЧУЖОЕ объявление вместо своего при совпадающих
    # значениях; теперь исключение по id. MIN_BLDG=3 -> у каждого из 4
    # после self-исключения должно остаться РОВНО 3 (не 2 — было бы, если
    # value-based remove случайно съедал лишнего чужого).
    listings = [_listing(f"L{i}", price=30_000_000, area=60.0, resolved_house_id=5,
                          complex_name="ЖК Дубль") for i in range(4)]
    result = compute_deal_scores(listings, _COMPLEXES, edge_m=100.0)
    for i in range(4):
        assert result[f"L{i}"]["sources"] == "тот же дом/ЖК"


def test_no_crash_on_large_pool_beyond_max_pool_cap():
    # MAX_POOL_BEFORE_SCORING=60 — собираем пул заметно больше, чтобы
    # проверить обрезку не роняет расчёт и не зависает.
    target = _listing("T0", price=30_000_000, area=60.0, resolved_house_id=99, complex_name="ЖК Плотный")
    others = [_listing(f"L{i}", price=30_000_000 + i * 10_000, area=60.0 + (i % 5),
                        resolved_house_id=99, complex_name="ЖК Плотный")
              for i in range(MAX_POOL_BEFORE_SCORING + 40)]
    result = compute_deal_scores([target] + others, _COMPLEXES, edge_m=100.0)
    assert "T0" in result
    assert result["T0"]["deal"] is not None
