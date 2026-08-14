"""Регрессия для задачи 2026-08-14 ("House-resolution в скоринге"):
quality-компонент Deal Score v4 должен брать housing_class/year_built
конкретного ДОМА (resolved_house_id), если он известен — а не зонтика,
чьим именем объявление всё ещё называется в тексте (complex_name).
bot/core/deal_score.compute_deal_scores() — чистая функция, без БД."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bot.core.deal_score import compute_deal_scores

UMBRELLA_ID = 900
HOUSE_ID = 901

_COMPLEXES_BY_NAME = {
    "жк тест": {"housing_class": "эконом", "year_built": 2018, "krisha_rating": None},
}
_COMPLEXES_BY_ID = {
    UMBRELLA_ID: {"housing_class": "эконом", "year_built": 2018, "krisha_rating": None},
    HOUSE_ID: {"housing_class": "элит", "year_built": 2026, "krisha_rating": None},
}


def _listing(id_, resolved_house_id=None):
    return {
        "id": id_, "lat": 51.10, "lon": 71.40, "price": 30_000_000, "area": 60.0,
        "rooms": 2, "floor": 5, "floors_total": 12, "year_built": None,
        "complex_name": "ЖК Тест", "is_owner": True, "district": "Есильский р-н",
        "yield_pct": 8.0, "same_complex_cnt": 1, "ceiling_height": None,
        "resolved_house_id": resolved_house_id,
    }


def test_quality_uses_house_class_when_resolved_house_id_present():
    listings = [_listing("A", resolved_house_id=HOUSE_ID)]
    result = compute_deal_scores(listings, _COMPLEXES_BY_NAME, edge_m=100.0,
                                 complexes_by_id=_COMPLEXES_BY_ID)
    quality_text = result["A"]["components"]["quality"]["text"]
    assert "элит" in quality_text
    assert "эконом" not in quality_text


def test_quality_falls_back_to_name_lookup_without_resolved_house_id():
    # Без resolved_house_id (обычный ЖК/сам зонтик) — поведение НЕ меняется,
    # как и раньше, по имени.
    listings = [_listing("B", resolved_house_id=None)]
    result = compute_deal_scores(listings, _COMPLEXES_BY_NAME, edge_m=100.0,
                                 complexes_by_id=_COMPLEXES_BY_ID)
    quality_text = result["B"]["components"]["quality"]["text"]
    assert "эконом" in quality_text


def test_quality_falls_back_when_complexes_by_id_not_passed():
    # Обратная совместимость: старые вызовы без complexes_by_id вообще
    # (None по умолчанию) — ведут себя ровно как до задачи.
    listings = [_listing("C", resolved_house_id=HOUSE_ID)]
    result = compute_deal_scores(listings, _COMPLEXES_BY_NAME, edge_m=100.0)
    quality_text = result["C"]["components"]["quality"]["text"]
    assert "эконом" in quality_text


def test_quality_falls_back_when_house_id_unknown_in_index():
    # resolved_house_id указывает на дом, которого почему-то нет в
    # complexes_by_id (напр. garbage-флаг убрал его из выборки на
    # бэкенде) — не падаем, тихо откатываемся на текстовый lookup.
    listings = [_listing("D", resolved_house_id=999_999)]
    result = compute_deal_scores(listings, _COMPLEXES_BY_NAME, edge_m=100.0,
                                 complexes_by_id=_COMPLEXES_BY_ID)
    quality_text = result["D"]["components"]["quality"]["text"]
    assert "эконом" in quality_text
