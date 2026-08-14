"""Регрессия для Фазы B, п.3 вердикт-стратегии (docs/verdict_strategy.md,
задача 2026-08-14): bot/core/housing_class_model — Gaussian Naive Bayes
на log(avg_price_m2)+year_built. Чистые функции, без БД."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bot.core.housing_class_model import (
    normalize_label, train, predict, evaluate_holdout, CLASSES,
)


def test_normalize_label_matches_canonical_classes():
    assert normalize_label("элит") == "элит"
    assert normalize_label("Элит-класс") == "элит"
    assert normalize_label("комфорт+") == "комфорт"
    assert normalize_label("бизнес класс") == "бизнес"


def test_normalize_label_unmappable_returns_none():
    # "премиум" не входит в 4-тиерную таксономию — честно исключается,
    # не гадаем, к какому из 4 классов его отнести.
    assert normalize_label("премиум") is None
    assert normalize_label(None) is None
    assert normalize_label("") is None


def _synthetic_dataset(n_per_class=30, seed=1):
    import numpy as np
    rng = np.random.default_rng(seed)
    # Классы разнесены по цене/м² широко (элит дороже эконом в разы) и
    # немного по году — синтетика, где модель ДОЛЖНА разделять почти
    # идеально, если реализация корректна.
    price_by_class = {"элит": 900_000, "бизнес": 600_000, "комфорт": 400_000, "эконом": 250_000}
    year_by_class = {"элит": 2023, "бизнес": 2020, "комфорт": 2015, "эконом": 2005}
    rows = []
    for cls in CLASSES:
        for _ in range(n_per_class):
            price = price_by_class[cls] * (1 + rng.normal(0, 0.05))
            year = int(year_by_class[cls] + rng.normal(0, 1))
            rows.append((cls, price, year))
    return rows


def test_train_predict_separates_well_on_synthetic_data():
    rows = _synthetic_dataset()
    model = train(rows)
    assert set(model["classes"].keys()) == set(CLASSES)
    # Явно элитная точка -> должна предсказаться как "элит" с высокой
    # вероятностью.
    cls, prob = predict(model, avg_price_m2=900_000, year_built=2023)
    assert cls == "элит"
    assert prob > 0.9
    # Явно эконом-точка -> "эконом".
    cls2, prob2 = predict(model, avg_price_m2=250_000, year_built=2005)
    assert cls2 == "эконом"
    assert prob2 > 0.9


def test_predict_none_when_features_missing():
    rows = _synthetic_dataset()
    model = train(rows)
    assert predict(model, None, 2020) == (None, None)
    assert predict(model, 500_000, None) == (None, None)
    assert predict(model, None, None) == (None, None)


def test_predict_none_when_model_untrained():
    empty_model = {"classes": {}, "n_total": 0}
    assert predict(empty_model, 500_000, 2020) == (None, None)


def test_evaluate_holdout_returns_high_accuracy_on_separable_synthetic_data():
    rows = _synthetic_dataset(n_per_class=40)
    report = evaluate_holdout(rows)
    assert report["n_holdout"] > 0
    assert report["accuracy"] > 0.85  # хорошо разделимые синтетические классы
    for cls in CLASSES:
        assert cls in report["per_class"]


def test_evaluate_holdout_small_class_goes_entirely_to_train():
    # "бизнес" — всего 3 примера (<5) -> целиком в train, 0 в holdout для
    # него; "элит" — 20, нормально сплитится. Не считаем метрику на
    # holdout-выборке размера 1, честно.
    rows = (
        [("элит", 900_000 * (1 + i * 0.001), 2020 + i % 5) for i in range(20)]
        + [("бизнес", 600_000, 2018), ("бизнес", 610_000, 2019), ("бизнес", 590_000, 2017)]
    )
    report = evaluate_holdout(rows)
    assert report["per_class"]["бизнес"]["n_holdout"] == 0
    assert report["per_class"]["элит"]["n_holdout"] > 0


def test_train_ignores_rows_with_missing_features():
    rows = [("элит", 900_000, 2023), ("элит", None, 2020), ("элит", 850_000, None)]
    model = train(rows)
    assert model["classes"]["элит"]["n"] == 1  # только первая строка валидна


def test_train_ignores_unmapped_class_keys():
    # Если вызывающий забыл нормализовать метку — train честно игнорирует
    # ключ, которого нет в CLASSES (защита, не молчаливая порча модели).
    rows = [("премиум", 1_000_000, 2023), ("элит", 900_000, 2023)]
    model = train(rows)
    assert "премиум" not in model["classes"]
    assert model["classes"]["элит"]["n"] == 1
