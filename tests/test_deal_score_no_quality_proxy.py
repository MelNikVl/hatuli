"""Регрессия для Фазы A, п.4 вердикт-стратегии (docs/verdict_strategy.md
§3.1 "Unknown ≠ average", задача 2026-08-14): при неизвестном
housing_class quality-компонент больше НЕ подставляет перцентиль цены/м²
как прокси-класс — раньше это создавало скрытую циклическую зависимость
(цена дважды влияла на score_total: напрямую через price-компонент и
косвенно через quality-прокси). bot/core/deal_score.compute_deal_scores()
— чистая функция, без БД."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bot.core.deal_score import compute_deal_scores

_COMPLEXES = {}  # класс ЖК неизвестен намеренно — это и есть тестируемая ветка


def _listing(id_, price, rooms=2, year_built=None, complex_name="ЖК Без Класса"):
    return {
        "id": id_, "lat": 51.10, "lon": 71.40, "price": price, "area": 60.0,
        "rooms": rooms, "floor": 5, "floors_total": 12, "year_built": year_built,
        "complex_name": complex_name, "is_owner": True, "district": "Есильский р-н",
        "yield_pct": 8.0, "same_complex_cnt": 1, "ceiling_height": None,
        "resolved_house_id": None, "finish_level": None,
    }


def test_quality_score_independent_of_price_when_class_unknown():
    # Раньше: дешёвая квартира получала низкий прокси-перцентиль в quality,
    # дорогая — высокий, при ОДИНАКОВОМ реальном качестве (год/рейтинг
    # неизвестны у обеих). Разные хексы (первая и вторая координата чуть
    # сдвинуты) — только чтобы не пересчитывать P_expected друг по другу,
    # сам факт координат не участвует в quality.
    cheap = compute_deal_scores(
        [_listing("cheap", price=15_000_000, complex_name="ЖК Дешёвый")],
        _COMPLEXES, edge_m=100.0)
    expensive = compute_deal_scores(
        [_listing("expensive", price=60_000_000, complex_name="ЖК Дорогой")],
        _COMPLEXES, edge_m=100.0)
    assert cheap["cheap"]["components"]["quality"]["score"] == \
        expensive["expensive"]["components"]["quality"]["score"]


def test_unknown_class_text_says_unknown_not_price_percentile():
    result = compute_deal_scores(
        [_listing("A", price=30_000_000, year_built=2020)], _COMPLEXES, edge_m=100.0)
    txt = result["A"]["components"]["quality"]["text"]
    assert "класс ЖК не известен" in txt
    assert "перцентиль" not in txt
    assert "цене/м²" not in txt


def test_fully_unknown_falls_back_to_flat_default_not_price():
    # Ничего не известно вообще (ни класса, ни года, ни рейтинга, ни
    # отделки) — честный дефолт 50 (не изменился этой задачей, отдельная
    # ветка), а не подмена ценой.
    result = compute_deal_scores([_listing("B", price=15_000_000)], _COMPLEXES, edge_m=100.0)
    assert result["B"]["components"]["quality"]["score"] == 50
    assert result["B"]["components"]["quality"]["text"] == "нет данных о ЖК (дефолт)"


def test_known_class_confidence_higher_than_unknown_no_partial_credit():
    complexes = {"жк известный": {"housing_class": "комфорт"}}
    known = compute_deal_scores(
        [_listing("K", price=30_000_000, complex_name="ЖК Известный")],
        complexes, edge_m=100.0)
    unknown = compute_deal_scores(
        [_listing("U", price=30_000_000, complex_name="ЖК Неизвестный")],
        _COMPLEXES, edge_m=100.0)
    # Раньше unknown получал частичную (+8) надбавку confidence за сам
    # факт, что цену можно было использовать для прокси — теперь ничего
    # не компенсирует отсутствие класса: разница минимум на полные 20
    # баллов (вес класса в confidence), не на 12 (20-8).
    assert known["K"]["confidence"] - unknown["U"]["confidence"] >= 20
