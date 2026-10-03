"""Архивация по last_seen (задача 2026-10-03): гейт здоровья deep sweep."""
from datetime import datetime, timedelta, timezone

from bot.core import stale_archive as sa

NOW = datetime(2026, 10, 3, 6, 0, tzinfo=timezone.utc)


def _s(completed_h_ago=10, duration_h=33.5, pages=1938, total=38688):
    return {'DEEP_SWEEP_CIRCLE_COMPLETED_AT': (NOW - timedelta(hours=completed_h_ago)).isoformat(),
            'DEEP_SWEEP_CIRCLE_DURATION_SEC': str(duration_h * 3600),
            'DEEP_SWEEP_CIRCLE_COMPLETED_PAGES': str(pages), 'DEEP_SWEEP_CIRCLE_COMPLETED_TOTAL': str(total)}


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


def test_rental_window_at_least_two_and_half_cycles():
    assert sa.rental_stale_days(26 * 3600) == 7          # круг ~сутки → 7 дн. минимум
    assert sa.rental_stale_days(5 * 86400) == 13         # круг 5 суток → 12.5 → 13


def test_rental_health_requires_measured_cycle():
    h, _ = sa.rental_health({}, NOW)
    assert not h.healthy
    ok, days = sa.rental_health({f'{sa.RENTAL_PREFIX}_COMPLETED_AT': (NOW - timedelta(hours=3)).isoformat(),
                                 f'{sa.RENTAL_PREFIX}_DURATION_SEC': str(26 * 3600)}, NOW)
    assert ok.healthy and days == 7
    stale, _ = sa.rental_health({f'{sa.RENTAL_PREFIX}_COMPLETED_AT': (NOW - timedelta(days=5)).isoformat(),
                                 f'{sa.RENTAL_PREFIX}_DURATION_SEC': str(26 * 3600)}, NOW)
    assert not stale.healthy


def test_half_catalog_circle_blocks_archiving():
    """Круг 958 стр. × 20 = 19 160 из 38 688 — половина каталога: «не видели» ≠ «ушло»."""
    h = sa.sweep_health(_s(pages=958), NOW)
    assert not h.healthy and 'покрыл' in h.reason


def test_unknown_coverage_blocks_archiving():
    s = _s(); del s['DEEP_SWEEP_CIRCLE_COMPLETED_PAGES']
    assert not sa.sweep_health(s, NOW).healthy
