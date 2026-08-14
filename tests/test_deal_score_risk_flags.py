"""Регрессия для Фазы A, п.6 вердикт-стратегии (docs/verdict_strategy.md
§5, задача 2026-08-14): risk перестаёт весово усредняться в score_total
(W_RISK=0, было 5%) — risk_bits идут отдельным списком флагов вердикта
(⚠-префикс) + два новых сигнала (мало аналогов, класс ЖК не известен).
price/quality/market перенормированы на освободившийся вес, пропорции
40:20:15 между собой сохранены. bot/core/deal_score.compute_deal_scores()
— чистая функция, без БД."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bot.core.deal_score import compute_deal_scores

_COMPLEXES = {"жк известный": {"housing_class": "комфорт", "year_built": 2020}}


def _listing(id_, floor=5, floors_total=12, is_owner=True, complex_name="ЖК Известный",
             price=30_000_000, area=60.0, rooms=2, lat=51.10, lon=71.40):
    return {
        "id": id_, "lat": lat, "lon": lon, "price": price, "area": area,
        "rooms": rooms, "floor": floor, "floors_total": floors_total, "year_built": 2020,
        "complex_name": complex_name, "is_owner": is_owner, "district": "Есильский р-н",
        "yield_pct": 8.0, "same_complex_cnt": 1, "ceiling_height": None,
        "resolved_house_id": None, "finish_level": None,
    }


def test_risk_weight_is_zero_not_folded_into_deal():
    result = compute_deal_scores([_listing("A")], _COMPLEXES, edge_m=100.0)
    assert result["A"]["components"]["risk"]["weight"] == 0.0


def test_first_floor_flag_appears_but_barely_moves_deal():
    # Раньше 1й этаж (-40 к risk_score) тонул в общем weighted average
    # (5% веса) — теперь не влияет на deal вообще (W_RISK=0), но флаг
    # должен появиться явно.
    ground = compute_deal_scores([_listing("A", floor=1)], _COMPLEXES, edge_m=100.0)
    mid = compute_deal_scores([_listing("B", floor=5)], _COMPLEXES, edge_m=100.0)
    assert "⚠ 1й этаж" in ground["A"]["flags"]
    assert ground["A"]["deal"] == mid["B"]["deal"]  # risk больше не двигает deal


def test_realtor_flag():
    result = compute_deal_scores([_listing("R", is_owner=False)], _COMPLEXES, edge_m=100.0)
    assert any("риелтор" in f for f in result["R"]["flags"])


def test_no_flags_when_clean():
    result = compute_deal_scores([_listing("C", floor=5, floors_total=12, is_owner=True)],
                                  _COMPLEXES, edge_m=100.0)
    assert not any("этаж" in f or "риелтор" in f for f in result["C"]["flags"])


def test_class_unknown_flag_present():
    result = compute_deal_scores([_listing("U", complex_name="ЖК Неизвестный")], {}, edge_m=100.0)
    assert "⚠ класс ЖК не известен" in result["U"]["flags"]


def test_class_known_no_unknown_flag():
    result = compute_deal_scores([_listing("K")], _COMPLEXES, edge_m=100.0)
    assert "⚠ класс ЖК не известен" not in result["K"]["flags"]


def test_few_comparables_flag_when_isolated():
    # Единственное объявление, свой ЖК/гекс/кольцо все пусты (кроме
    # самого себя) — expected_raw целиком на городской медиане ("только
    # город") -> флаг "мало аналогов".
    result = compute_deal_scores([_listing("L", complex_name="Одинокий ЖК")],
                                  _COMPLEXES, edge_m=100.0)
    assert "⚠ мало аналогов" in result["L"]["flags"]


def test_many_comparables_no_few_comparables_flag():
    # MIN_BLDG=3 (bot/core/hedonic_constants.py) — 4 объявления того же
    # ЖК/сегмента/похожей площади достаточно, чтобы own_bldg сработал.
    listings = [_listing(f"S{i}", complex_name="ЖК Плотный", price=p)
                for i, p in enumerate([30_000_000, 31_000_000, 29_000_000, 30_500_000])]
    result = compute_deal_scores(listings, _COMPLEXES, edge_m=100.0)
    assert "⚠ мало аналогов" not in result["S0"]["flags"]


def test_price_quality_market_weights_keep_proportions_summing_to_one():
    result = compute_deal_scores([_listing("P")], _COMPLEXES, edge_m=100.0)
    comp = result["P"]["components"]
    total = comp["price"]["weight"] + comp["location"]["weight"] + \
        comp["quality"]["weight"] + comp["market"]["weight"] + comp["risk"]["weight"]
    assert abs(total - 1.0) < 1e-9
    # 40:20:15 -> после нормировки без risk(5%) и location(0%) остаётся
    # 0.75 суммарно, пропорции друг к другу не меняются.
    assert abs(comp["price"]["weight"] / comp["quality"]["weight"] - 40 / 20) < 1e-9
    assert abs(comp["quality"]["weight"] / comp["market"]["weight"] - 20 / 15) < 1e-9
