"""Isolated rental budget/counters/archive regressions; no live DB or HTTP."""
import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import service_rental as service
from bot.core import archive_check, apartment_details
from bot.core.rental_service_metrics import (
    MaintenanceBudget, RentalCyclePlan, daily_counter_payload, record_rental_cycle,
    source_failure_counters,
)


def operations(collect=None):
    return service.RentalOperations(
        collect or AsyncMock(return_value={
            "page": 2, "saved": 40, "completed": False,
            "head_pages": 1, "durable_pages": 1, "requests": 2,
            "new": 3, "updated": 37, "reactivated": 1,
        }), AsyncMock(), AsyncMock(), AsyncMock(),
        AsyncMock(return_value={"requests": 1, "blocked": 0}),
        AsyncMock(return_value={"requests": 3, "archived": 1}),
        AsyncMock(), AsyncMock(), AsyncMock(),
    )


@pytest.mark.asyncio
async def test_failed_sheet_upload_cannot_refresh_success_timestamp(monkeypatch):
    monkeypatch.setattr(service, 'record_rental_cycle', AsyncMock())
    state = service.RentalServiceState(pages_since_index=14,index_refreshes=2)
    ops = operations()
    ops.sync_sheets.return_value = False
    cycle = await service.run_one_cycle(None,'/arenda/kvartiry/astana/','apartment',state,ops,RentalCyclePlan())
    assert cycle['stages']['sheets']['status']=='error'
    assert all(call.args[0]!='SHEETS_RENTAL_SYNCED_AT' for call in ops.set_setting.await_args_list)


def test_maintenance_limits_preserve_disabled_settings_and_reject_unbounded_values():
    default = RentalCyclePlan.from_settings(lambda key, default: default)
    assert default.backfill_batch == 2 and default.archive_batch == 3
    assert default.maintenance_seconds == 45 and not default.allow_geo_complex_guess
    values = {"RENTAL_MAINTENANCE_BUDGET_SEC": "inf", "RENTAL_BACKFILL_BATCH": "0",
              "RENTAL_ARCHIVE_CHECK_BATCH": "900", "RENTAL_DB_MAINTENANCE_EVERY": "-2"}
    plan = RentalCyclePlan.from_settings(lambda key, default: values.get(key, default))
    assert plan.maintenance_seconds == 45
    assert plan.backfill_batch == 0 and plan.archive_batch == 3 and plan.db_every == 1


@pytest.mark.asyncio
async def test_list_runs_first_hot_count_does_not_advance_index_twice(monkeypatch):
    persisted = AsyncMock()
    monkeypatch.setattr(service, "record_rental_cycle", persisted)
    state = service.RentalServiceState(pages_since_index=13)
    ops = operations()
    events = []
    async def collect(*args, **kwargs):
        events.append("list")
        return {"page": 2, "saved": 40, "completed": False,
                "head_pages": 1, "durable_pages": 1, "requests": 2,
                "new": 3, "updated": 37}
    async def backfill(*args, **kwargs):
        events.append("backfill")
        assert kwargs["budget_seconds"] <= 30
        return {"requests": 1}
    ops.collect = collect
    ops.backfill = backfill
    await service.run_one_cycle(None, "/rental/", "apartment", state, ops, RentalCyclePlan())
    assert events[0] == "list" and events.index("backfill") > 0
    assert state.pages_since_index == 14
    ops.rebuild_index.assert_not_awaited()
    cycle, counters = persisted.await_args.args
    assert cycle["head_pages"] == 1 and cycle["durable_pages"] == 1
    assert counters["pages"] == 1 and counters["head_pages"] == 1
    assert counters["new"] == 3 and counters["list_requests"] == 2


@pytest.mark.asyncio
async def test_source_block_keeps_cursor_progress_and_stops_detail_maintenance(monkeypatch):
    persisted = AsyncMock()
    monkeypatch.setattr(service, "record_rental_cycle", persisted)
    request = httpx.Request("GET", "https://krisha.kz/arenda/test/")
    exc = httpx.HTTPStatusError("468", request=request, response=httpx.Response(468, request=request))
    exc.rental_metrics = {"head_pages": 1, "saved": 20, "new": 2, "updated": 18, "requests": 2}
    ops = operations(AsyncMock(side_effect=exc))
    state = service.RentalServiceState(pages_since_index=10)
    result = await service.run_one_cycle(None, "/rental/", "apartment", state, ops, RentalCyclePlan())
    assert result["list_status"] == "blocked" and state.pages_since_index == 10
    ops.backfill.assert_not_awaited()
    ops.archive.assert_not_awaited()
    counters = persisted.await_args.args[1]
    assert counters["http_468"] == 1 and counters["blocked"] == 1
    assert counters["head_pages"] == 1 and counters["new"] == 2
    assert counters["saved_observations"] == 20
    assert counters.get("pages", 0) == 0


@pytest.mark.asyncio
async def test_timeout_preserves_archive_observations_and_next_list_cycle(monkeypatch):
    persisted = AsyncMock()
    monkeypatch.setattr(service, "record_rental_cycle", persisted)
    state = service.RentalServiceState(cycle=1)
    ops = operations()
    async def slow_archive(*args, progress, **kwargs):
        progress.update(requests=1, archived=1)
        await asyncio.Event().wait()
    ops.archive = slow_archive
    plan = RentalCyclePlan(maintenance_seconds=1.05, backfill_batch=0)
    cycle = await service.run_one_cycle(None, "/rental/", "parking", state, ops, plan)
    assert cycle["stages"]["archive"]["status"] == "timeout"
    assert persisted.await_args.args[1]["archived"] == 1
    assert persisted.await_args.args[1]["archive_requests"] == 1
    ops.archive = AsyncMock(return_value={"requests": 1})
    await service.run_one_cycle(None, "/rental/", "commercial", state, ops, plan)
    assert ops.collect.await_count == 2


@pytest.mark.asyncio
async def test_index_refresh_only_resets_after_success_and_geoguess_off(monkeypatch):
    monkeypatch.setattr(service, "record_rental_cycle", AsyncMock())
    state = service.RentalServiceState(cycle=4, pages_since_index=14)
    ops = operations()
    ops.rebuild_index.side_effect = RuntimeError("index unavailable")
    await service.run_one_cycle(None, "/rental/", "apartment", state, ops, RentalCyclePlan())
    assert state.pages_since_index == 15 and state.index_refreshes == 0
    ops.bind_address.assert_awaited_once_with("rental_listings")
    ops.bind_geo.assert_not_awaited()
    ops.rebuild_index.side_effect = None
    await service.run_one_cycle(None, "/rental/", "parking", state, ops, RentalCyclePlan())
    assert state.pages_since_index == 0 and state.index_refreshes == 1


@pytest.mark.asyncio
async def test_budget_does_not_start_stage_when_consumed():
    now = [0.0]
    budget = MaintenanceBudget(45, clock=lambda: now[0])
    op = AsyncMock()
    now[0] = 45
    assert await budget.run("archive", lambda _: op()) is None
    op.assert_not_awaited()
    assert budget.stages["archive"]["status"] == "budget_exhausted"


@pytest.mark.asyncio
async def test_circle_estimate_uses_success_intervals_and_requires_known_boundary(monkeypatch):
    monkeypatch.setattr(service, "record_rental_cycle", AsyncMock())
    now = [100.0]
    state = service.RentalServiceState()
    ops = operations(AsyncMock(return_value={"page": 1, "saved": 20, "completed": False,
                                           "requests": 1, "expected_last_page": 200}))
    plan = RentalCyclePlan(maintenance_seconds=0)
    first = await service.run_one_cycle(None, "/rental/", "apartment", state, ops, plan, clock=lambda: now[0])
    assert first["estimated_circle_hours"] is None
    now[0] = 400
    second = await service.run_one_cycle(None, "/rental/", "apartment", state, ops, plan, clock=lambda: now[0])
    assert second["page_interval_seconds"] == 300
    assert second["estimated_circle_hours"] == 16.67
    assert second["page_interval_samples"] == 1
    ops.set_setting.assert_any_await("RENTAL_CRAWL_V1_APARTMENT_ESTIMATED_HOURS", "16.67")


def test_counters_are_allowlisted_and_distinguish_network_error_from_source_block():
    payload = daily_counter_payload({"new": -3, "pages": "4", "updated": float("nan"), "contacts": 99})
    assert payload["new"] == 0 and payload["pages"] == 4 and payload["updated"] == 0
    assert "contacts" not in payload
    assert source_failure_counters(ValueError("unrecognized page")) == {"blocked": 0, "http_468": 0, "errors": 1}


@pytest.mark.asyncio
async def test_counter_update_is_atomic_and_uses_local_day_without_schema_changes(monkeypatch):
    from bot.db import pg
    execute = AsyncMock()
    monkeypatch.setattr(pg, "execute", execute)
    await record_rental_cycle({"list_status": "ok"}, {"new": 7, "pages": 1},
                              now=datetime(2026, 10, 13, 21, tzinfo=timezone.utc))
    first = execute.await_args_list[0]
    assert first.args[1] == "RENTAL_METRICS_V1_2026-10-14"
    assert "ON CONFLICT" in first.args[0] and "jsonb_object_agg" in first.args[0]
    assert "CREATE" not in first.args[0] and "ALTER" not in first.args[0]
    assert json.loads(first.args[2])["new"] == 7


def test_logging_does_not_duplicate_redirected_systemd_log(monkeypatch):
    monkeypatch.setattr(service.os, "stat", lambda _: SimpleNamespace(st_ino=55, st_dev=1))
    monkeypatch.setattr(service.os, "fstat", lambda _: SimpleNamespace(st_ino=55, st_dev=1))
    monkeypatch.setattr(service.sys, "stdout", SimpleNamespace(fileno=lambda: 1))
    monkeypatch.setattr(service.sys, "stderr", SimpleNamespace(fileno=lambda: 2))
    capture = {}
    monkeypatch.setattr(service.logging, "basicConfig", lambda **kwargs: capture.update(kwargs))
    service.configure_logging()
    assert len(capture["handlers"]) == 1


def archive_fixture(monkeypatch, serve):
    monkeypatch.setattr(apartment_details, "_BLOCKED_UNTIL", 0.0)
    monkeypatch.setattr(apartment_details, "_BLOCKED_STREAK", 0)
    monkeypatch.setattr(archive_check, "fetch", AsyncMock(return_value=[
        {"id": "1", "url": "https://krisha.kz/a/show/1"},
        {"id": "2", "url": "https://krisha.kz/a/show/2"},
    ]))
    execute = AsyncMock()
    monkeypatch.setattr(archive_check, "execute", execute)
    monkeypatch.setattr(archive_check.asyncio, "sleep", AsyncMock())
    real_client = httpx.AsyncClient
    monkeypatch.setattr(archive_check.httpx, "AsyncClient", lambda **kwargs:
                        real_client(transport=httpx.MockTransport(serve), **kwargs))
    return execute


@pytest.mark.asyncio
@pytest.mark.parametrize("status,text", [(468, "blocked"), (429, "blocked"),
                                      (200, "Подтвердите, что вы не робот")])
async def test_rental_archive_stops_first_block_without_archiving(monkeypatch, status, text):
    visits = []
    def serve(request):
        visits.append(str(request.url))
        return httpx.Response(status, text=text)
    execute = archive_fixture(monkeypatch, serve)
    result = await archive_check.check_archived_rentals(limit=60, budget_seconds=20)
    assert len(visits) == 1 and result["requests"] == 1
    assert result["blocked"] == 1 and result["http_468"] == int(status == 468)
    execute.assert_not_awaited()
    assert apartment_details.detail_fetch_cooldown_remaining() > 0


@pytest.mark.asyncio
async def test_rental_archive_cooldown_skips_sleep_and_database(monkeypatch):
    execute = archive_fixture(monkeypatch, lambda request: httpx.Response(200))
    monkeypatch.setattr(apartment_details, "detail_fetch_cooldown_remaining", lambda: 900)
    result = await archive_check.check_archived_rentals(limit=3)
    assert result["requests"] == 0 and result["skipped_cooldown"]
    archive_check.fetch.assert_not_awaited()
    archive_check.asyncio.sleep.assert_not_awaited()
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_rental_archive_insufficient_budget_does_not_issue_request(monkeypatch):
    visits = []
    execute = archive_fixture(monkeypatch, lambda request: visits.append(request) or httpx.Response(404))
    result = await archive_check.check_archived_rentals(limit=3, budget_seconds=4)
    assert result["requests"] == 0 and result["budget_exhausted"]
    assert not visits
    execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,text", [(503, "unavailable"), (200, "<html>upstream error</html>")])
async def test_rental_archive_unknown_response_never_confirms_freshness(monkeypatch, status, text):
    execute = archive_fixture(monkeypatch, lambda request: httpx.Response(status, text=text))
    result = await archive_check.check_archived_rentals(limit=3, budget_seconds=20)
    assert result["errors"] == 2 and result["checked"] == 0 and result["archived"] == 0
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_rental_archive_does_not_count_already_archived_race_twice(monkeypatch):
    execute = archive_fixture(monkeypatch, lambda request: httpx.Response(404))
    execute.return_value = "UPDATE 0"
    result = await archive_check.check_archived_rentals(limit=3, budget_seconds=20)
    assert result["checked"] == 2 and result["archived"] == 0


@pytest.mark.asyncio
async def test_rental_archive_only_confirmed_404_changes_state_no_age_heuristic(monkeypatch):
    execute = archive_fixture(monkeypatch, lambda request: httpx.Response(404))
    progress = {}
    result = await archive_check.check_archived_rentals(limit=3, budget_seconds=20, progress=progress)
    assert result is progress and result["archived"] == 2
    for call in execute.await_args_list:
        sql, lid, reason = call.args
        assert "UPDATE rental_listings" in sql and "DELETE" not in sql
        assert "COALESCE(archived_at, now())" in sql and reason == "confirmed_gone"
        assert "last_seen =" not in sql
