"""Пороги страницы «Качество данных» (задача 2026-10-03)."""
from datetime import date, datetime, timedelta, timezone

from bot.core import data_quality as dq

NOW = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)


def test_coverage_grades():
    assert dq.grade_coverage('complex_id', 85.1) == 'ok'
    assert dq.grade_coverage('complex_id', 75) == 'warn'
    assert dq.grade_coverage('complex_id', 10) == 'bad'
    assert dq.grade_coverage('views', None) == 'bad'


def test_freshness_grades():
    t = date(2026, 10, 3)
    assert dq.grade_age('daily', t, t) == 'ok'
    assert dq.grade_age('daily', t - timedelta(days=2), t) == 'warn'
    assert dq.grade_age('daily', t - timedelta(days=5), t) == 'bad'
    assert dq.grade_age('monthly', t - timedelta(days=30), t) == 'ok'
    assert dq.grade_age('weekly', None, t) == 'bad'


def test_cycle_grades():
    assert dq.grade_cycle(NOW - timedelta(hours=10), 33.5, NOW, 48) == 'ok'
    assert dq.grade_cycle(NOW - timedelta(hours=10), 60, NOW, 48) == 'warn'      # круг дольше цели
    assert dq.grade_cycle(NOW - timedelta(days=8), 33.5, NOW, 48) == 'bad'       # обход встал
    assert dq.grade_cycle(None, None, NOW, 36) == 'bad'


def test_every_coverage_key_has_threshold():
    assert set(dq.COVERAGE_THRESHOLDS) == {'coords', 'complex_id', 'housing_class', 'views',
                                           'deal_score_today', 'liquidity_today'}
