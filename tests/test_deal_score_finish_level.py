"""Регрессия для задачи 2026-08-14 ("finish_level: убрать тихий инкремент"):
отделка теперь небольшой субкомпонент quality внутри Deal Score v4 (виден
в breakdown), не прямая невидимая правка score_total. bot/core/deal_score.
compute_deal_scores() — чистая функция, без БД."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bot.core.deal_score import compute_deal_scores

_COMPLEXES = {}  # ЖК неизвестен намеренно — изолируем эффект именно finish_level


def _listing(id_, finish_level=None):
    return {
        "id": id_, "lat": 51.10, "lon": 71.40, "price": 30_000_000, "area": 60.0,
        "rooms": 2, "floor": 5, "floors_total": 12, "year_built": None,
        "complex_name": "ЖК Без Данных", "is_owner": True, "district": "Есильский р-н",
        "yield_pct": 8.0, "same_complex_cnt": 1, "ceiling_height": None,
        "resolved_house_id": None, "finish_level": finish_level,
    }


def test_finish_level_appears_in_quality_breakdown_text():
    result = compute_deal_scores([_listing("A", finish_level="designer")], _COMPLEXES, edge_m=100.0)
    assert "дизайнерский ремонт" in result["A"]["components"]["quality"]["text"]


def test_finish_level_none_does_not_appear_in_breakdown():
    result = compute_deal_scores([_listing("B", finish_level=None)], _COMPLEXES, edge_m=100.0)
    assert "ремонт" not in result["B"]["components"]["quality"]["text"]
    assert "черновая" not in result["B"]["components"]["quality"]["text"]


def test_designer_finish_scores_higher_quality_than_rough():
    designer = compute_deal_scores([_listing("C", finish_level="designer")], _COMPLEXES, edge_m=100.0)
    rough = compute_deal_scores([_listing("D", finish_level="rough")], _COMPLEXES, edge_m=100.0)
    assert designer["C"]["components"]["quality"]["score"] > rough["D"]["components"]["quality"]["score"]


def test_finish_level_weight_is_small_not_dominant():
    # "небольшой вес" — designer (95, максимум шкалы) против отсутствия
    # сигнала вообще, в синтетике БЕЗ класса/года/рейтинга ЖК (самый
    # щедрый для finish_level случай, реальные ЖК почти всегда добавляют
    # другие сигналы и разбавляют его долю ещё сильнее) — разница в
    # итоговом score_total заметно меньше типичного разброса Deal Score
    # (0-100), не переворачивает скор с ног на голову.
    #
    # Верхняя граница поднята 10→15 задачей 2026-08-14 (Фаза A п.4
    # вердикт-стратегии, "убрать price→quality proxy"): раньше "without"
    # (ни класса, ни года, ни рейтинга, ни отделки) в этом ОДНОлистинговом
    # синтетическом вызове доставал квази-прокси по цене вырожденного вида
    # (city_sorted из одного элемента → перцентиль строго 0) — baseline
    # quality занижался артефактом, не честным сигналом. Теперь "without"
    # честно падает на плоский дефолт 50 (см. test_deal_score_no_quality_
    # proxy.py), delta считается против реального baseline, не против
    # проксированного нуля — эффект отделки (11 баллов) не изменился по
    # существу, изменилась точка отсчёта.
    with_finish = compute_deal_scores([_listing("E", finish_level="designer")], _COMPLEXES, edge_m=100.0)
    without = compute_deal_scores([_listing("F", finish_level=None)], _COMPLEXES, edge_m=100.0)
    delta = with_finish["E"]["deal"] - without["F"]["deal"]
    assert 0 <= delta <= 15


def test_unknown_finish_code_ignored_gracefully():
    # Код не из словаря (например, будущее расширение _FINISH_RULES,
    # которое забыли добавить сюда) — не роняет расчёт, просто не
    # участвует, как отсутствие сигнала.
    result = compute_deal_scores([_listing("G", finish_level="some_future_code")], _COMPLEXES, edge_m=100.0)
    assert result["G"]["deal"] is not None
