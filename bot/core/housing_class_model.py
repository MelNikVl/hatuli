"""Класс-модель ЖК (Фаза B, п.3, задача 2026-08-14, docs/verdict_strategy.md).

**НАЗНАЧЕНИЕ (честно ограничено)**: подготовка данных для будущей
стратификации аналогов (`comparable_score.py`) и ML-моделей Фазы C — НЕ
попытка поднять `price_score` AUC прямо сейчас. Тот вопрос уже проверен
честным `as_of`-backtest Фазы B п.2 ("Анализ потолка price_score",
`docs/verdict_strategy.md`) — бутылочное горлышко предсказания не в
качестве пула аналогов, класс-модель его не адресует и не должна
оцениваться по этому критерию.

**Метод**: Gaussian Naive Bayes на ДВУХ признаках — `log(avg_price_m2)`
и `year_built`. Единственные два с широким покрытием среди `complexes`
(~88%/84% живых ЖК на дату задачи) — `has_parking`/`has_security`/
`has_closed_territory` (<2% заполнено), `complex_tech_specs` (высота
потолков — 5 строк на всю базу), `housing_class_test` (лифты/квартиры —
64%, но admin-only эксперимент сомнительного качества, `scoring_audit.md`
§1) — все слишком разрежены или ненадёжны для первой версии, не
включены (не гадаем на плохих данных). Не sklearn (недоступен в
окружении) — прозрачная ручная реализация на numpy: тот же принцип
объяснимости, что Deal Score v4 (breakdown вместо чёрного ящика).

**НЕ путать с `complexes.housing_class_estimate`** (Часть 2 п.11,
`scoring_roadmap.md`) — тот разовый heuristic-бэкфил (правило по
медианной цене/м²+потолкам, заморожен с 2026-08-01), не модель, не
обучается, не даёт вероятности. Эта модель — `predicted_housing_class`
(миграция `071_predicted_housing_class.sql`), обучаема заново по мере
роста разметки, с честной calibrated-вероятностью.
"""
from __future__ import annotations

import math

import numpy as np

from bot.core.hedonic_constants import _CLASS_SCORE, _class_key

CLASSES: tuple[str, ...] = tuple(_CLASS_SCORE.keys())  # ("элит", "бизнес", "комфорт", "эконом")


def normalize_label(raw: str | None) -> str | None:
    """Сырой текст housing_class -> один из CLASSES, или None, если не
    маппится (например "премиум" — не входит в 4-тиерную таксономию,
    используемую everywhere в проекте, единичные случаи честно
    исключаются из обучения, не гадаем, к какому классу их отнести).
    Переиспользует _class_key() — ту же нормализацию, что deal_score.py/
    comparable_score.py, не вторую параллельную."""
    return _class_key((raw or "").lower())


def _features(avg_price_m2: float | None, year_built: int | None) -> np.ndarray | None:
    """[log(avg_price_m2), year_built] — None, если хоть один признак
    неизвестен (Unknown ≠ average, docs/verdict_strategy.md §3.1 — не
    подставляем среднее по городу за неизвестную цену/год)."""
    if not avg_price_m2 or avg_price_m2 <= 0 or not year_built:
        return None
    return np.array([math.log(float(avg_price_m2)), float(year_built)])


def train(labeled: list[tuple[str, float, int]]) -> dict:
    """labeled: [(class_key, avg_price_m2, year_built), ...] — метки уже
    нормализованы (normalize_label применён заранее вызывающим, None
    отфильтрованы). Возвращает модель: {"classes": {cls: {mean, std, n,
    prior}}, "n_total": N} — станд. Gaussian Naive Bayes, независимые
    признаки, диагональная ковариация."""
    by_class: dict[str, list[np.ndarray]] = {c: [] for c in CLASSES}
    for cls, price, year in labeled:
        feat = _features(price, year)
        if feat is not None and cls in by_class:
            by_class[cls].append(feat)

    n_total = sum(len(v) for v in by_class.values())
    model: dict = {"classes": {}, "n_total": n_total}
    for cls, feats in by_class.items():
        n = len(feats)
        if n == 0:
            continue
        arr = np.array(feats)
        mean = arr.mean(axis=0)
        # ddof=0 — не делим на (n-1)=0 при n=1; минимальный std (эпсилон)
        # — не даём распределению схлопнуться в точку у классов с
        # единичными примерами (переобучение на 1 наблюдении).
        std = np.maximum(arr.std(axis=0, ddof=0), 1e-6)
        model["classes"][cls] = {"mean": mean, "std": std, "n": n, "prior": n / n_total if n_total else 0.0}
    return model


def _log_gaussian_pdf(x: np.ndarray, mean: np.ndarray, std: np.ndarray) -> float:
    return float(np.sum(-0.5 * np.log(2 * math.pi * std ** 2) - (x - mean) ** 2 / (2 * std ** 2)))


def predict(model: dict, avg_price_m2: float | None, year_built: int | None) -> tuple[str | None, float | None]:
    """-> (predicted_class, probability). (None, None), если признаки
    неизвестны или модель не обучена ни на одном классе."""
    feat = _features(avg_price_m2, year_built)
    if feat is None or not model["classes"]:
        return None, None
    log_post = {
        cls: math.log(params["prior"]) + _log_gaussian_pdf(feat, params["mean"], params["std"])
        for cls, params in model["classes"].items()
        if params["prior"] > 0
    }
    if not log_post:
        return None, None
    # Softmax по log-posterior -> честные вероятности (сумма=1 по
    # классам, для которых модель вообще обучена).
    max_log = max(log_post.values())
    exp_vals = {c: math.exp(lp - max_log) for c, lp in log_post.items()}
    total = sum(exp_vals.values())
    probs = {c: v / total for c, v in exp_vals.items()}
    best_cls = max(probs, key=probs.get)
    return best_cls, probs[best_cls]


def evaluate_holdout(labeled: list[tuple[str, float, int]], seed: int = 20260814,
                      holdout_frac: float = 0.2) -> dict:
    """Стратифицированный train/holdout сплит (по классам — редкие
    классы не должны пропасть из holdout целиком). Классы с <5 примерами
    целиком идут в train (holdout из 0-1 примеров не даёт значимой
    метрики, честно не оцениваем такой класс на holdout, а не считаем
    accuracy на выборке размера 1). Не пишет в БД — чистая функция."""
    rng = np.random.default_rng(seed)
    by_class: dict[str, list[tuple[str, float, int]]] = {}
    for row in labeled:
        by_class.setdefault(row[0], []).append(row)

    train_rows: list[tuple[str, float, int]] = []
    holdout_rows: list[tuple[str, float, int]] = []
    for cls, rows in by_class.items():
        if len(rows) < 5:
            train_rows.extend(rows)
            continue
        idx = rng.permutation(len(rows))
        n_holdout = max(1, int(len(rows) * holdout_frac))
        holdout_idx = set(idx[:n_holdout].tolist())
        for i, row in enumerate(rows):
            (holdout_rows if i in holdout_idx else train_rows).append(row)

    model = train(train_rows)
    correct = 0
    per_class = {c: {"tp": 0, "fp": 0, "fn": 0} for c in CLASSES}
    for true_cls, price, year in holdout_rows:
        pred_cls, _prob = predict(model, price, year)
        if pred_cls == true_cls:
            correct += 1
            per_class[true_cls]["tp"] += 1
        else:
            per_class[true_cls]["fn"] += 1
            if pred_cls is not None:
                per_class[pred_cls]["fp"] += 1

    n_holdout = len(holdout_rows)
    accuracy = correct / n_holdout if n_holdout else None
    per_class_metrics = {}
    for cls, counts in per_class.items():
        tp, fp, fn = counts["tp"], counts["fp"], counts["fn"]
        precision = tp / (tp + fp) if (tp + fp) else None
        recall = tp / (tp + fn) if (tp + fn) else None
        per_class_metrics[cls] = {"precision": precision, "recall": recall, "n_holdout": tp + fn}

    return {
        "n_train": len(train_rows), "n_holdout": n_holdout,
        "accuracy": accuracy, "per_class": per_class_metrics, "model": model,
    }
