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


def test_circle_coverage():
    # Planned page spans and legacy counters are never source coverage.
    assert dq.circle_coverage_pct({'DEEP_SWEEP_CIRCLE_MAX_PAGE': '958', 'KRISHA_TOTAL_FOUND': '38688'}) is None
    assert dq.circle_coverage_pct({'DEEP_SWEEP_CIRCLE_MAX_PAGE': '1938', 'KRISHA_TOTAL_FOUND': '38688'}) is None
    assert dq.circle_coverage_pct({}) is None
    assert dq.worst('ok', 'bad', 'warn') == 'bad'


def test_circle_coverage_uses_actual_unique_ids_not_planned_or_completed_page_projections():
    from bot.core.sale_sweep import SaleSweep, COMPLETED_KEY
    proof = SaleSweep.new(NOW - timedelta(hours=20))
    ids = [str(100000 + n) for n in range(20)]
    proof.account([{"page": 1, "ids": ids, "accepted_ids": ids,
                    "checked_at": NOW.isoformat(), "total_count": 40,
                    "terminal_reason": "explicit_last_page"}], ids, now=NOW)
    settings = {COMPLETED_KEY: proof.dumps(), "DEEP_SWEEP_CIRCLE_COMPLETED_PAGES": "2000",
                "DEEP_SWEEP_CIRCLE_MAX_PAGE": "2000", "KRISHA_TOTAL_FOUND": "40"}
    assert dq.circle_coverage_pct(settings) == 50.0
