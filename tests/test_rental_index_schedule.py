"""Actual collector-cycle freshness, retry and partial-save contracts; no I/O."""
import asyncio
from unittest.mock import AsyncMock

import pytest

import service_rental as service
from bot.core.rental_service_metrics import RentalCyclePlan


MONTHLY = "/arenda/kvartiry/astana/"
DAILY = "/arenda/kvartiry-posutochno/astana/"


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setattr(service, "record_rental_cycle", AsyncMock())
    now = [100.0]
    plan = RentalCyclePlan(backfill_batch=0, archive_batch=0, db_every=30)
    ops = service.RentalOperations(
        AsyncMock(return_value={"page": 1, "saved": 20, "completed": False,
            "head_pages": 1, "durable_pages": 1, "requests": 1}),
        *[AsyncMock() for _ in range(8)])
    state = service.RentalServiceState()

    async def run(path=MONTHLY, prop_type="apartment", **kwargs):
        return await service.run_one_cycle(None, path, prop_type, state, ops,
            kwargs.pop("plan", plan), clock=lambda: now[0], **kwargs)

    return state, ops, now, run


@pytest.mark.asyncio
async def test_saved_daily_data_refreshes_on_next_healthy_step_after_five_minutes(setup):
    state, ops, now, run = setup
    await run(DAILY)
    assert state.pending_index_observations == 20
    now[0] += 299
    await run("/arenda/garazhi/astana/", "parking")
    ops.rebuild_index.assert_not_awaited()
    assert state.apartment_pages_since_index == 1
    now[0] += 1
    cycle = await run("/arenda/kommercheskaya-nedvizhimost/astana/", "commercial")
    ops.rebuild_index.assert_awaited_once()
    assert ops.collect.await_count == 3
    assert cycle["index"]["due_reason"] == "saved_observations_age"
    assert cycle["index"]["pending_observations"] == 0
    assert cycle["index"]["last_success_age_seconds"] == 0


@pytest.mark.asyncio
async def test_parking_and_commercial_pages_do_not_trigger_apartment_index(setup):
    state, ops, now, run = setup
    state.pages_since_index = 14
    await run("/arenda/garazhi/astana/", "parking")
    await run("/arenda/kommercheskaya-nedvizhimost/astana/", "commercial")
    assert state.pages_since_index == 16  # Existing diagnostic count survives.
    assert state.pending_index_observations == 0
    assert state.apartment_pages_since_index == 0
    ops.rebuild_index.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_apartment_response_does_not_invent_dirty_market(setup):
    state, ops, now, run = setup
    ops.collect.return_value["saved"] = 0
    state.apartment_pages_since_index = 14
    await run()
    now[0] += 3600
    await run()
    assert state.index_dirty_since is None
    assert state.apartment_pages_since_index == 14
    ops.rebuild_index.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", [False, True])
async def test_partial_saves_survive_failed_source_and_wait_for_healthy_budget(setup, blocked):
    state, ops, now, run = setup
    error = RuntimeError("deep page failed after saving the head")
    error.source_blocked = blocked
    error.rental_metrics = {"saved": 12, "head_pages": 1, "requests": 2}
    ops.collect.side_effect = error
    cycle = await run(DAILY)
    assert cycle["list_status"] == ("blocked" if blocked else "error")
    assert state.pending_index_observations == 12
    assert state.index_dirty_since == 100
    assert state.apartment_pages_since_index == 0
    now[0] += 301
    cycle = await run(DAILY)
    assert cycle["stages"]["index"]["status"] == "budget_exhausted"
    assert state.pending_index_observations == 24
    assert state.index_last_attempt is None
    ops.rebuild_index.assert_not_awaited()
    ops.collect.side_effect = None
    await run("/arenda/garazhi/astana/", "parking")
    ops.rebuild_index.assert_awaited_once()
    assert state.pending_index_observations == 0


@pytest.mark.asyncio
async def test_zero_budget_retains_due_work_without_starting_retry_cooldown(setup):
    state, ops, now, run = setup
    state.index_refresh_required = True
    cycle = await run(plan=RentalCyclePlan(maintenance_seconds=0))
    assert cycle["stages"]["index"]["status"] == "budget_exhausted"
    assert state.index_last_attempt is None and state.index_refresh_required
    assert state.pending_index_observations == 20
    await run()
    ops.rebuild_index.assert_awaited_once()
    assert not state.index_refresh_required


@pytest.mark.asyncio
async def test_failed_rebuild_retains_dirty_work_and_backs_off_without_skipping_lists(setup):
    state, ops, now, run = setup
    state.index_refresh_required = True
    ops.rebuild_index.side_effect = RuntimeError("database transient")
    cycle = await run()
    assert cycle["stages"]["index"]["status"] == "error"
    assert state.index_failures == 1 and state.pending_index_observations == 20
    now[0] += 59
    cycle = await run()
    assert "index" not in cycle["stages"]
    assert cycle["index"]["retry_after_seconds"] == 1
    now[0] += 1
    await run()
    assert state.index_failures == 2
    now[0] += 119
    await run()
    assert ops.rebuild_index.await_count == 2
    now[0] += 1
    ops.rebuild_index.side_effect = None
    await run()
    assert ops.collect.await_count == 5
    assert ops.rebuild_index.await_count == 3
    assert state.index_failures == 0 and state.pending_index_observations == 0


@pytest.mark.asyncio
async def test_success_throttle_keeps_new_data_pending_even_if_page_threshold_is_hit(setup):
    state, ops, now, run = setup
    state.index_refresh_required = True
    await run(index_refresh_pages=1)
    now[0] += 1
    await run(index_refresh_pages=1)
    assert ops.rebuild_index.await_count == 1
    assert state.pending_index_observations == 20
    now[0] += 59
    await run(index_refresh_pages=1)
    assert ops.rebuild_index.await_count == 2


@pytest.mark.asyncio
async def test_timed_indexes_do_not_increase_external_sheet_export_frequency(setup):
    state, ops, now, run = setup
    state.index_refresh_required = True
    await run()
    for _ in range(3):
        now[0] += 301
        await run()
        now[0] += 301
        await run("/arenda/garazhi/astana/", "parking")
    assert state.index_refreshes >= 3
    assert state.pages_since_sheets == 7
    ops.sync_sheets.assert_not_awaited()


@pytest.mark.asyncio
async def test_due_sheet_export_survives_failure_and_resets_only_after_real_success(setup):
    state, ops, now, run = setup
    state.index_refresh_required = True
    state.pages_since_sheets = 44
    ops.sync_sheets.return_value = False
    cycle = await run()
    assert cycle["stages"]["sheets"]["status"] == "error"
    assert state.pages_since_sheets == 45
    assert not any(call.args[0] == "SHEETS_RENTAL_SYNCED_AT"
                   for call in ops.set_setting.await_args_list)
    ops.sync_sheets.return_value = True
    now[0] += 300
    await run()
    now[0] += 300
    await run("/arenda/garazhi/astana/", "parking")
    assert ops.sync_sheets.await_count == 2
    assert state.pages_since_sheets == 0
    assert sum(call.args[0] == "SHEETS_RENTAL_SYNCED_AT"
               for call in ops.set_setting.await_args_list) == 1


@pytest.mark.asyncio
async def test_cancelled_rebuild_does_not_acknowledge_unsuccessful_refresh(setup):
    state, ops, now, run = setup
    state.index_refresh_required = True
    ops.rebuild_index.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await run()
    assert state.pending_index_observations == 20
    assert state.index_refresh_required and state.index_refreshes == 0
    assert state.index_last_success is None


@pytest.mark.asyncio
async def test_timed_out_rebuild_keeps_pending_work_and_consumes_no_extra_source_calls(setup):
    state, ops, now, run = setup
    state.index_refresh_required = True

    async def stuck_rebuild():
        await asyncio.Event().wait()

    ops.rebuild_index.side_effect = stuck_rebuild
    cycle = await run(plan=RentalCyclePlan(maintenance_seconds=1.01,
        backfill_batch=0, archive_batch=0))
    assert cycle["stages"]["index"]["status"] == "timeout"
    assert state.index_failures == 1 and state.pending_index_observations == 20
    assert state.index_last_success is None and state.index_refresh_required
    assert ops.collect.await_count == 1
    now[0] += 60
    ops.rebuild_index.side_effect = None
    await run()
    assert state.index_refreshes == 1 and state.pending_index_observations == 0


def test_refresh_setting_is_finite_and_bounded():
    def plan(value):
        return RentalCyclePlan.from_settings(
            lambda key, default: value if key == "RENTAL_INDEX_REFRESH_SEC" else default)
    assert plan("nan").index_refresh_seconds == 300
    assert plan("0").index_refresh_seconds == 60
    assert plan("999999").index_refresh_seconds == 1800
    assert plan("450").index_refresh_seconds == 450
