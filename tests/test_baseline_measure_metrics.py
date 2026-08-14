"""Регрессия для Фазы A.5, п.5 вердикт-стратегии (docs/verdict_strategy.md,
задача 2026-08-14) — метрические функции baseline_measure.py v2 (AUC,
PR-AUC/average_precision, lift@k/precision@k, calibration_deciles).
Чистые функции на numpy, без БД."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import pytest

from baseline_measure import (
    auc_mannwhitney, average_precision, lift_and_precision_at_k, calibration_deciles,
)


def test_auc_perfect_separation():
    scores = np.array([10, 20, 30, 40], dtype=float)
    labels = np.array([False, False, True, True])
    auc, n_pos, n_neg = auc_mannwhitney(scores, labels)
    assert auc == 1.0
    assert n_pos == 2 and n_neg == 2


def test_auc_inverse_separation():
    scores = np.array([10, 20, 30, 40], dtype=float)
    labels = np.array([True, True, False, False])
    auc, _, _ = auc_mannwhitney(scores, labels)
    assert auc == 0.0


def test_auc_exact_half():
    # positives={10,40}, negatives={20,30}: (10<20 neg,10<30 neg,40>20 pos,
    # 40>30 pos) -> 2/4 побед позитива -> AUC ровно 0.5.
    scores = np.array([10, 20, 30, 40], dtype=float)
    labels = np.array([True, False, False, True])
    auc, _, _ = auc_mannwhitney(scores, labels)
    assert auc == 0.5


def test_auc_none_when_one_class_empty():
    scores = np.array([10, 20, 30], dtype=float)
    labels = np.array([False, False, False])
    auc, n_pos, n_neg = auc_mannwhitney(scores, labels)
    assert auc is None
    assert n_pos == 0


def test_average_precision_perfect_ranking():
    # Все TRUE выше всех FALSE -> AP = 1.0
    scores = np.array([40, 30, 20, 10], dtype=float)
    labels = np.array([True, True, False, False])
    ap = average_precision(scores, labels)
    assert ap == 1.0


def test_average_precision_worst_ranking():
    # Все FALSE выше всех TRUE -> AP низкий (не 1.0)
    scores = np.array([40, 30, 20, 10], dtype=float)
    labels = np.array([False, False, True, True])
    ap = average_precision(scores, labels)
    assert ap < 0.6


def test_average_precision_none_when_degenerate():
    scores = np.array([1.0, 2.0, 3.0])
    assert average_precision(scores, np.array([False, False, False])) is None
    assert average_precision(scores, np.array([True, True, True])) is None


def test_lift_and_precision_at_k_top_all_positive():
    # 10 объектов, топ-10%=1 объект; он TRUE -> precision@10%=1.0,
    # base_rate=0.3 -> lift ~3.33
    scores = np.arange(10, dtype=float)
    labels = np.array([False]*7 + [True]*3)  # base_rate=0.3, топ-скор (9) -> label True (последний)
    prec, lift, k = lift_and_precision_at_k(scores, labels, k_frac=0.1)
    assert k == 1
    assert prec == 1.0
    assert lift == pytest.approx(1.0 / 0.3)


def test_lift_below_one_when_topk_worse_than_base_rate():
    scores = np.arange(10, dtype=float)
    labels = np.array([True]*3 + [False]*7)  # позитивы внизу по скору
    prec, lift, k = lift_and_precision_at_k(scores, labels, k_frac=0.1)
    assert prec == 0.0
    assert lift == 0.0


def test_calibration_deciles_monotonic_for_perfect_signal():
    n = 100
    scores = np.arange(n, dtype=float)
    labels = np.array([i >= 80 for i in range(n)])  # только топ-20% TRUE
    deciles = calibration_deciles(scores, labels)
    assert len(deciles) == 10
    rates = [r for _, _, r, _ in deciles]
    # Дециль 9 и 10 (последние 20%) -> rate=1.0, остальные -> 0.0
    assert rates[-1] == 1.0
    assert rates[-2] == 1.0
    assert all(r == 0.0 for r in rates[:-2])


def test_calibration_deciles_covers_all_rows():
    n = 47
    scores = np.random.default_rng(1).random(n)
    labels = np.random.default_rng(2).random(n) > 0.5
    deciles = calibration_deciles(scores, labels)
    assert sum(cnt for _, cnt, _, _ in deciles) == n


def test_tie_breaking_consistent_between_topk_and_decile():
    # Регрессия на живой баг (Фаза A.5 п.5): много связок (одинаковый
    # скор) со СИЛЬНО НЕслучайным относительно исхода порядком строк —
    # "топ-10%" (argsort по убыванию) и "дециль 10" (argsort по
    # возрастанию + разбивка) раньше выбирали РАЗНЫЕ подмножества одной
    # связки, потому что argsort стабилен и сохраняет порядок строк на
    # обоих концах связки по-разному. Здесь: 100 объектов, все скор=50
    # (одна сплошная связка), первые 80 (по исходному порядку) — FALSE,
    # последние 20 — TRUE. Без перемешивания decile-10 включил бы почти
    # только TRUE (0.8-1.0), а precision@10% (argsort(-s), тот же
    # стабильный порядок с начала связки) — почти только FALSE (~0.0) —
    # то есть один и тот же вырожденный сигнал ("все скоры одинаковы,
    # сигнала нет") показал бы противоречивые "то 0, то почти 1"
    # метрики. Тест воспроизводит СЫРЫЕ (не перемешанные) функции —
    # baseline_measure.py._report_signal сам перемешивает перед вызовом,
    # здесь проверяется, что БЕЗ перемешивания расхождение
    # действительно есть (документирует, почему перемешивание в
    # _report_signal обязательно, а не просто перестраховка).
    n = 100
    scores = np.full(n, 50.0)
    labels = np.array([False] * 80 + [True] * 20)
    prec10, _, k = lift_and_precision_at_k(scores, labels, k_frac=0.1)
    deciles = calibration_deciles(scores, labels)
    decile10_rate = deciles[-1][2]
    # На вырожденных полностью равных скорах ("сигнала нет") эти две
    # метрики технически ДОЛЖНЫ давать один и тот же (близкий) результат
    # — оба должны отражать base_rate=0.2, а не показывать один 0 другой
    # ~1. Именно это расхождение и было живым багом (сейчас документируем
    # его наличие в СЫРЫХ функциях, чтобы объяснить, зачем в
    # _report_signal — обязательное перемешивание).
    assert prec10 == 0.0  # первые (по исходному порядку) k строк - все False
    assert decile10_rate == 1.0  # последние (по исходному порядку) строки - все True
    assert prec10 != decile10_rate  # именно это расхождение и есть баг без перемешивания
