"""Архивация по last_seen (задача 2026-10-03): гейт здоровья deep sweep."""
from datetime import datetime, timedelta, timezone

from bot.core import stale_archive as sa

NOW = datetime(2026, 10, 3, 6, 0, tzinfo=timezone.utc)


def _s(completed_h_ago=10, duration_h=33.5):
    return {'DEEP_SWEEP_CIRCLE_COMPLETED_AT': (NOW - timedelta(hours=completed_h_ago)).isoformat(),
            'DEEP_SWEEP_CIRCLE_DURATION_SEC': str(duration_h * 3600)}


def test_healthy_sweep():
    h = sa.sweep_health(_s(), NOW)
    assert h.healthy and h.reason == 'ok'


def test_no_completed_circle_blocks():
    assert not sa.sweep_health({}, NOW).healthy


def test_stalled_sweep_blocks():
    """Парсер стоит 4 дня — архивировать нельзя, иначе уйдёт весь рынок."""
    assert not sa.sweep_health(_s(completed_h_ago=96), NOW).healthy


def test_too_slow_circle_blocks():
    """Круг 4 дня: в окно 7 дней не влезает два круга — сигнал «не видели» ненадёжен."""
    assert not sa.sweep_health(_s(duration_h=96), NOW).healthy


def test_naive_timestamp_treated_as_utc():
    s = _s(); s['DEEP_SWEEP_CIRCLE_COMPLETED_AT'] = (NOW - timedelta(hours=5)).replace(tzinfo=None).isoformat()
    assert sa.sweep_health(s, NOW).healthy


def test_garbage_settings_do_not_crash():
    h = sa.sweep_health({'DEEP_SWEEP_CIRCLE_COMPLETED_AT': 'x', 'DEEP_SWEEP_CIRCLE_DURATION_SEC': 'y'}, NOW)
    assert not h.healthy


def test_cutoff_is_stale_days():
    assert NOW - sa.stale_cutoff(NOW) == timedelta(days=sa.STALE_DAYS)


def test_migration_allows_new_reason():
    from pathlib import Path
    sql = (Path(__file__).resolve().parents[1] / 'migrations' / '098_sweep_stale_archive_and_sale_labels.sql').read_text()
    assert "'sweep_stale'" in sql and "'archived_badge'" in sql and "'confirmed_gone'" in sql
