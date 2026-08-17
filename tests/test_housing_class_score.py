"""Регрессия для задачи 2026-08-17 ("исправить концепцию housing class,
но пока не менять production-веса"): bot/core/housing_class_score.py —
admin-only тестовый скор (/admin/analytics/complexes, "Класс жилья") —
apartment_count убран как положительный сигнал ("больше квартир -> выше
класс" не обоснована; многоподъездные масс-маркет ЖК систематически
содержат БОЛЬШЕ квартир, чем компактные премиум-дома)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_apartment_count_not_in_weights():
    from bot.core.housing_class_score import WEIGHTS
    assert "apartment_count" not in WEIGHTS


def test_apartment_count_does_not_change_score():
    """Две ЖК, идентичные по всем учитываемым метрикам, различаются
    ТОЛЬКО apartment_count — скор должен быть одинаковым (сигнал не
    учитывается вовсе, не просто "низкий вес")."""
    from bot.core.housing_class_score import compute_housing_class_scores

    rows = [
        {"id": 1, "name": "A", "price_per_m2": 500000, "ceiling_height": 3.0,
         "floors_total": 5, "apartment_count": 40},
        {"id": 2, "name": "B", "price_per_m2": 500000, "ceiling_height": 3.0,
         "floors_total": 5, "apartment_count": 400},
    ]
    out = compute_housing_class_scores(rows)
    assert out[0]["score"] == out[1]["score"]
    assert "apartment_count" not in out[0]["score_details"]
    assert "apartment_count" not in out[1]["score_details"]


def test_score_still_computed_from_remaining_metrics():
    from bot.core.housing_class_score import compute_housing_class_scores

    rows = [
        {"id": 1, "name": "Дешёвый", "price_per_m2": 300000, "ceiling_height": 2.7, "floors_total": 20},
        {"id": 2, "name": "Дорогой", "price_per_m2": 900000, "ceiling_height": 3.2, "floors_total": 5},
    ]
    out = compute_housing_class_scores(rows)
    assert out[0]["score"] is not None
    assert out[1]["score"] is not None
    assert out[1]["score"] > out[0]["score"]  # дороже + выше потолки + меньше этажей -> выше класс


def test_missing_all_metrics_gives_none_score():
    from bot.core.housing_class_score import compute_housing_class_scores

    rows = [{"id": 1, "name": "Пусто"}]
    out = compute_housing_class_scores(rows)
    assert out[0]["score"] is None
