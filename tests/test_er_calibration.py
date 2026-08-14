"""Регрессия для Фазы B, п.4 вердикт-стратегии (docs/verdict_strategy.md,
задача 2026-08-14): bot/core/er_calibration — чистые агрегирующие функции
для отчёта калибровки ER-порогов. Без БД."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from bot.core.er_calibration import (
    summarize_confidence_distribution, unit_gold_label_confirmation_rate,
    evidence_confirmation_breakdown,
)


def test_summarize_confidence_distribution_tiers():
    confidences = [0.51, 0.6, 0.79, 0.8, 0.95, 1.0, 0.3]
    report = summarize_confidence_distribution(confidences, auto_threshold=0.8, review_threshold=0.5)
    assert report["n"] == 7
    assert report["auto_tier"] == 3   # 0.8, 0.95, 1.0
    assert report["review_tier"] == 3  # 0.51, 0.6, 0.79
    assert report["below_review"] == 1  # 0.3


def test_summarize_confidence_distribution_empty():
    report = summarize_confidence_distribution([], auto_threshold=0.8, review_threshold=0.5)
    assert report["n"] == 0
    assert report["buckets"] == {}


def test_summarize_confidence_distribution_buckets_sorted():
    confidences = [0.78, 0.52, 0.61, 0.55]
    report = summarize_confidence_distribution(confidences, auto_threshold=0.8, review_threshold=0.5)
    keys = list(report["buckets"].keys())
    assert keys == sorted(keys)


def test_unit_gold_label_confirmation_rate_all_approve():
    report = unit_gold_label_confirmation_rate(["approve"] * 43)
    assert report["n"] == 43
    assert report["approve"] == 43
    assert report["approve_rate"] == 1.0
    assert report["other"] == 0


def test_unit_gold_label_confirmation_rate_mixed():
    report = unit_gold_label_confirmation_rate(["approve", "approve", "reject"])
    assert report["approve_rate"] == pytest.approx(2 / 3)


def test_unit_gold_label_confirmation_rate_empty():
    report = unit_gold_label_confirmation_rate([])
    assert report["n"] == 0
    assert report["approve_rate"] is None


def test_evidence_confirmation_breakdown():
    evidence = [
        {"price_ok": True, "date_ok": True},
        {"price_ok": True, "date_ok": False},
        {"price_ok": False, "date_ok": False},
        {"price_ok": False, "date_ok": True},
    ]
    report = evidence_confirmation_breakdown(evidence)
    assert report["n"] == 4
    assert report["price_ok"] == 2
    assert report["date_ok"] == 2
    assert report["neither"] == 1
