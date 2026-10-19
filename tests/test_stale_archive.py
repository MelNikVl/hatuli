"""Архивация по last_seen (задача 2026-10-03): гейт здоровья deep sweep."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from bot.core import stale_archive as sa

NOW = datetime(2026, 10, 3, 6, 0, tzinfo=timezone.utc)


def _s(completed_h_ago=10, duration_h=33.5, pages=1938, total=38688):
    from bot.core.sale_sweep import SaleSweep, COMPLETED_KEY
    done = NOW - timedelta(hours=completed_h_ago)
    proof = SaleSweep.new(done - timedelta(hours=duration_h))
    reports = []
    for page in range(1, pages + 1):
        ids = [str(100000 + (page - 1) * 20 + i) for i in range(20)]
        reports.append({"page": page, "ids": ids, "accepted_ids": ids,
                        "checked_at": done.isoformat(), "total_count": total,
                        "terminal_reason": "explicit_last_page" if page == pages else None})
    proof.account(reports, [i for r in reports for i in r["ids"]], now=done)
    return {'DEEP_SWEEP_CIRCLE_COMPLETED_AT': (NOW - timedelta(hours=completed_h_ago)).isoformat(),
            'DEEP_SWEEP_CIRCLE_DURATION_SEC': str(duration_h * 3600),
            'DEEP_SWEEP_CIRCLE_COMPLETED_PAGES': str(pages), 'DEEP_SWEEP_CIRCLE_COMPLETED_TOTAL': str(total),
            COMPLETED_KEY: proof.dumps()}


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


def test_legacy_timestamp_and_planned_pages_cannot_authorize_archiving():
    from bot.core.sale_sweep import COMPLETED_KEY
    s = _s()
    del s[COMPLETED_KEY]
    assert not sa.sweep_health(s, NOW).healthy


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
                                 f'{sa.RENTAL_PREFIX}_DURATION_SEC': str(26 * 3600),
                                 f'{sa.RENTAL_PREFIX}_COMPLETED_VERIFIED': '1',
                                 f'{sa.RENTAL_PREFIX}_LAST_PAGES': '207',
                                 f'{sa.RENTAL_PREFIX}_UNIQUE_SEEN': '4131',
                                 f'{sa.RENTAL_PREFIX}_TOTAL': '4131',
                                 f'{sa.RENTAL_PREFIX}_LAST_SUCCESS_AT': NOW.isoformat()}, NOW)
    assert ok.healthy and days == 7
    stale, _ = sa.rental_health({f'{sa.RENTAL_PREFIX}_COMPLETED_AT': (NOW - timedelta(days=5)).isoformat(),
                                 f'{sa.RENTAL_PREFIX}_DURATION_SEC': str(26 * 3600)}, NOW)
    assert not stale.healthy


def test_rental_legacy_completed_time_cannot_authorize_inferred_archiving():
    h, _ = sa.rental_health({f'{sa.RENTAL_PREFIX}_COMPLETED_AT': NOW.isoformat(),
                            f'{sa.RENTAL_PREFIX}_DURATION_SEC': '3600'}, NOW)
    assert not h.healthy and 'старый курсор' in h.reason


def test_rental_incomplete_coverage_blocks_archiving_even_if_parser_is_running():
    settings = {f'{sa.RENTAL_PREFIX}_COMPLETED_AT': NOW.isoformat(),
                f'{sa.RENTAL_PREFIX}_DURATION_SEC': '3600',
                f'{sa.RENTAL_PREFIX}_COMPLETED_VERIFIED': '1',
                f'{sa.RENTAL_PREFIX}_LAST_PAGES': '207',
                f'{sa.RENTAL_PREFIX}_UNIQUE_SEEN': '2000',
                f'{sa.RENTAL_PREFIX}_TOTAL': '4131',
                f'{sa.RENTAL_PREFIX}_LAST_SUCCESS_AT': NOW.isoformat()}
    h, _ = sa.rental_health(settings, NOW)
    assert not h.healthy and '95%' in h.reason


def test_half_catalog_circle_blocks_archiving():
    """Круг 958 стр. × 20 = 19 160 из 38 688 — половина каталога: «не видели» ≠ «ушло»."""
    h = sa.sweep_health(_s(pages=958), NOW)
    assert not h.healthy and 'покрыл' in h.reason


def test_unknown_coverage_blocks_archiving():
    from bot.core.sale_sweep import COMPLETED_KEY
    s = _s(); del s[COMPLETED_KEY]
    assert not sa.sweep_health(s, NOW).healthy


@pytest.mark.asyncio
async def test_sale_archive_excludes_recent_unpriced_source_ids_without_touching_price_freshness(monkeypatch):
    from bot.core.sale_sweep import SaleSweep, COMPLETED_KEY
    from bot.db import pg
    settings = _s(pages=3, total=60)
    proof = SaleSweep.loads(settings[COMPLETED_KEY])
    proof.pages['1']['saved_ids'].remove('100000')
    proof.pages['1']['rejected'] = {'missing_price': 1}
    settings[COMPLETED_KEY] = proof.dumps()
    monkeypatch.setattr(sa, '_settings', AsyncMock(return_value=settings))
    count = AsyncMock(return_value=1)
    update = AsyncMock(return_value='UPDATE 1')
    monkeypatch.setattr(pg, 'fetchval', count)
    monkeypatch.setattr(pg, 'execute', update)
    result = await sa.archive_stale(now=NOW)
    assert result['healthy'] and result['archived'] == 1
    assert count.call_args.args[-1] == ['100000']
    assert update.call_args.args[-1] == ['100000']
    assert 'NOT(id=ANY($3::text[]))' in update.call_args.args[0]
    assert 'last_seen=' not in update.call_args.args[0]


@pytest.mark.asyncio
async def test_sale_archive_reads_atomic_evidence_keys(monkeypatch):
    from bot.core.sale_sweep import STATE_KEY, COMPLETED_KEY
    from bot.db import pg
    fetch = AsyncMock(return_value=[])
    monkeypatch.setattr(pg, 'fetch', fetch)
    assert await sa._settings() == {}
    assert STATE_KEY in fetch.call_args.args[-1] and COMPLETED_KEY in fetch.call_args.args[-1]
