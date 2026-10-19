"""Unreadable current evidence cannot authorize removal using an older pass."""
import json
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from bot.core.sale_sweep import SaleSweep, STATE_KEY, COMPLETED_KEY
from bot.core.stale_archive import sweep_health
from service_apartments import _collect_sale_catalog, _commit_sale_catalog
from tests.test_sale_sweep import NOW, Settings, completed_journal, receipt


@pytest.mark.parametrize("raw", ["{}", "not json", '{"version":2}'])
def test_corrupt_current_journal_holds_archive_even_with_healthy_completed_pass(raw):
    settings = {COMPLETED_KEY:completed_journal().dumps(), STATE_KEY:raw}
    result = sweep_health(settings, NOW)
    assert not result.healthy and "повреждён" in result.reason


@pytest.mark.asyncio
async def test_replacement_journal_preserves_hold_across_restart_until_complete_source_and_save_proof():
    settings = Settings({COMPLETED_KEY:completed_journal().dumps(), STATE_KEY:"not json"})
    analyze = AsyncMock(return_value=[])
    _, state, _, _, _ = await _collect_sale_catalog(settings, analyze, now=NOW)
    assert state.archive_hold_reason == "current_journal_unreadable"
    assert state.archive_hold_started_at == NOW.isoformat()
    assert not sweep_health(settings.values,NOW).healthy
    # Restarting with a valid replacement is not itself replacement coverage.
    _, restored, _, _, _ = await _collect_sale_catalog(settings, analyze, now=NOW+timedelta(minutes=1))
    assert restored.archive_hold_reason == state.archive_hold_reason
    assert restored.archive_hold_started_at == state.archive_hold_started_at
    first = receipt(1,total=40)
    await _commit_sale_catalog(settings,restored,[first],first["accepted_ids"],{}, {},now=NOW+timedelta(minutes=2))
    assert not sweep_health(settings.values,NOW+timedelta(minutes=2)).healthy
    second = receipt(2,total=40,terminal=True)
    # The terminal page alone cannot clear a database-save gap.
    await _commit_sale_catalog(settings,restored,[second],[],{}, {},now=NOW+timedelta(minutes=3))
    assert restored.archive_hold_reason and not restored.closed
    await _commit_sale_catalog(settings,restored,[second],second["accepted_ids"],{}, {},now=NOW+timedelta(minutes=4))
    assert restored.closed and restored.archive_hold_reason is None
    assert restored.archive_hold_started_at is None
    assert sweep_health(settings.values,NOW+timedelta(minutes=4)).healthy


@pytest.mark.asyncio
async def test_absent_legacy_v1_state_starts_normally_without_claiming_old_planned_coverage():
    settings = Settings({"DEEP_SWEEP_PAGE":"900","DEEP_SWEEP_CIRCLE_MAX_PAGE":"1900"})
    _,state,_,_,_ = await _collect_sale_catalog(settings,AsyncMock(return_value=[]),now=NOW)
    assert state.next_page == 1 and state.archive_hold_reason is None
    assert not sweep_health(settings.values,NOW).healthy


def test_invalid_hold_provenance_and_completed_but_held_receipt_are_not_valid_evidence():
    data = json.loads(completed_journal().dumps())
    data.update(archive_hold_reason="current_journal_unreadable",archive_hold_started_at=NOW.isoformat())
    assert SaleSweep.loads(json.dumps(data)) is None
    data["completed_at"] = None
    data["archive_hold_started_at"] = None
    assert SaleSweep.loads(json.dumps(data)) is None
