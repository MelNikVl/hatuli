#!/usr/bin/env python3
"""Rental lists first; sequential enrichment has an explicit cycle budget."""
from __future__ import annotations

import asyncio
import logging
import os
import random
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from dotenv import load_dotenv
from bot.core.rental_service_metrics import (
    MaintenanceBudget, RentalCyclePlan, record_rental_cycle, source_failure_counters,
)

load_dotenv()
log = logging.getLogger("rental_service")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")
RENTAL_SHEETS_SYNC_PAGES = 45
RENTAL_INDEX_REVALIDATION_SECONDS = 900.0


def configure_logging() -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    # Production redirects stdout/stderr to rental.log. A FileHandler pointed
    # at the same inode previously wrote every event twice.
    redirected = False
    try:
        target = os.stat("rental.log")
        redirected = any(os.fstat(stream.fileno()).st_ino == target.st_ino
                         and os.fstat(stream.fileno()).st_dev == target.st_dev
                         for stream in (sys.stdout, sys.stderr))
    except (OSError, ValueError, AttributeError):
        pass
    if not redirected:
        handlers.append(logging.FileHandler("rental.log", encoding="utf-8", errors="replace"))
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=handlers)


def service_revision() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True,
            stderr=subprocess.DEVNULL, timeout=3).strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


@dataclass
class RentalServiceState:
    cycle: int = 0
    pages_since_index: int = 0
    pages_since_sheets: int = 0
    index_refreshes: int = 0
    completed_types: set[str] = field(default_factory=set)
    last_success: dict[str, float] = field(default_factory=dict)
    page_intervals: dict[str, float] = field(default_factory=dict)
    visit_intervals: dict[str, float] = field(default_factory=dict)
    visit_pages: dict[str, float] = field(default_factory=dict)
    interval_samples: dict[str, int] = field(default_factory=dict)
    detail_turn: int = 0
    # Keep the original all-category page counter for diagnostics. Only
    # apartment saves/pages determine apartment-index freshness.
    apartment_pages_since_index: int = 0
    pending_index_observations: int = 0
    index_dirty_since: float | None = None
    index_last_attempt: float | None = None
    index_last_success: float | None = None
    index_failures: int = 0
    index_refresh_required: bool = False

    def note_apartment_saves(self, saved: int, pages: int, now: float) -> None:
        if saved <= 0:
            return
        if self.index_dirty_since is None:
            self.index_dirty_since = now
        self.pending_index_observations += saved
        self.apartment_pages_since_index += pages

    def index_retry_after(self, now: float) -> float:
        if self.index_last_attempt is None:
            return 0.0
        # Successes cannot cause a hot-loop rebuild; failures back off to at
        # most five minutes. Budget exhaustion does not count as an attempt.
        delay = min(300.0, 60.0 * 2 ** min(max(self.index_failures - 1, 0), 3))
        return max(0.0, self.index_last_attempt + delay - now)

    def index_due_reason(self, now: float, *, pages: int, seconds: float,
                         full_pass: bool) -> str | None:
        if self.index_retry_after(now):
            return None
        if self.index_refresh_required:
            return "startup"
        # A completed catalog pass and periodic revalidation also retire
        # aggregates invalidated by archives, deduplication or admin changes.
        # These source changes need not include a newly saved apartment card.
        if full_pass:
            return "full_pass"
        if (self.index_last_success is not None
                and now - self.index_last_success >= RENTAL_INDEX_REVALIDATION_SECONDS):
            return "periodic_revalidation"
        if self.pending_index_observations <= 0:
            return None
        if self.apartment_pages_since_index >= pages:
            return "apartment_pages"
        if self.index_dirty_since is not None and now - self.index_dirty_since >= seconds:
            return "saved_observations_age"
        return None


@dataclass
class RentalOperations:
    collect: Callable
    rebuild_index: Callable
    sync_sheets: Callable
    dedup: Callable
    backfill: Callable
    archive: Callable
    bind_address: Callable
    bind_geo: Callable
    set_setting: Callable


async def run_one_cycle(client, path: str, prop_type: str, state: RentalServiceState,
                        operations: RentalOperations, plan: RentalCyclePlan, *,
                        type_count: int = 3, index_refresh_pages: int = 15,
                        sleep_seconds: float = 120.0, clock=time.monotonic) -> dict:
    """One list step and bounded maintenance; callers always rotate types."""
    state.cycle += 1
    # Monthly and daily apartments have independent durable cursors and timing.
    # Preserve legacy monthly metric names used by collection-health consumers.
    from bot.core.rental_period import catalog_cursor_key, catalog_period
    cursor_prefix = catalog_cursor_key(prop_type, path)
    scope_key = cursor_prefix.removeprefix('RENTAL_CRAWL_V1_').lower()
    started = clock()
    list_started = clock()
    step = {}
    counters = {"cycles": 1}
    list_status = "ok"
    try:
        step = await operations.collect(client, path, prop_type, refresh_head=True,
            max_durable_pages=plan.list_batch_pages,
            head_min_interval_seconds=plan.head_refresh_seconds)
        log.info("[%s] page %d: saved %d; completed=%s; head_pages=%d; durable_pages=%d",
                 prop_type, step["page"], step["saved"], step["completed"],
                 step.get("head_pages", 0), step.get("durable_pages", 1))
        durable_pages = step.get("durable_pages", 1)
        state.pages_since_index += durable_pages
        state.pages_since_sheets += durable_pages
        if step["completed"]:
            state.completed_types.add(scope_key)
        counters.update({
            "pages": durable_pages, "head_pages": step.get("head_pages", 0),
            "saved_observations": step["saved"],
            "list_requests": step.get("requests", durable_pages + step.get("head_pages", 0)),
            "new": step.get("new", 0), "updated": step.get("updated", 0),
            "reactivated": step.get("reactivated", 0),
            "raw_cards": step.get("raw_cards", 0), "accepted_cards": step.get("accepted_cards", 0),
            "unpriced_cards": step.get("unpriced_cards", 0),
        })
    except Exception as exc:
        list_status = "blocked" if source_failure_counters(exc)["blocked"] else "error"
        counters.update(source_failure_counters(exc))
        partial = getattr(exc, "rental_metrics", {})
        step = partial
        counters.update({key: partial.get(key, 0) for key in (
            "head_pages", "new", "updated", "reactivated", "raw_cards", "accepted_cards", "unpriced_cards")})
        counters["saved_observations"] = partial.get("saved", 0)
        counters["list_requests"] = partial.get("requests", 1)
        counters["pages"] = partial.get("durable_pages", 0)
        state.pages_since_index += counters["pages"]
        state.pages_since_sheets += counters["pages"]
        log.error("[%s] rental list step %s: %s", prop_type, list_status, exc,
                  exc_info=list_status != "blocked")
    list_seconds = max(0.0, clock() - list_started)
    if prop_type == "apartment":
        # A saved head or partially persisted page still changes the market
        # even when the following request fails. Keep it pending while the
        # existing source-block gate prevents maintenance in this cycle.
        state.note_apartment_saves(counters.get("saved_observations", 0),
                                   counters.get("pages", 0), clock())
    estimated_circle_hours = None
    if counters.get("pages", 0):
        previous_success = state.last_success.get(scope_key)
        successful_at = clock()
        if previous_success is not None and successful_at > previous_success:
            elapsed = successful_at - previous_success
            old_seconds = state.visit_intervals.get(scope_key, elapsed)
            old_pages = state.visit_pages.get(scope_key, counters["pages"])
            state.visit_intervals[scope_key] = .25 * elapsed + .75 * old_seconds
            state.visit_pages[scope_key] = .25 * counters["pages"] + .75 * old_pages
            state.page_intervals[scope_key] = round(
                state.visit_intervals[scope_key] / state.visit_pages[scope_key], 3)
            state.interval_samples[scope_key] = state.interval_samples.get(scope_key, 0) + 1
        state.last_success[scope_key] = successful_at
        last_page = step.get("expected_last_page")
        if last_page and scope_key in state.page_intervals:
            estimated_circle_hours = round(last_page * state.page_intervals[scope_key] / 3600, 2)
    # A blocked list is not a reason to send detail traffic in the same cycle.
    budget = MaintenanceBudget(plan.maintenance_seconds if list_status == "ok" else 0.0,
                               clock=clock)
    def database_cap(maximum):
        # Keep a usable request window on detail-priority turns. A local
        # index/rule job must not consume the entire source-refresh budget.
        reserve = 30. if plan.backfill_batch and state.cycle % 2 else 0.
        return min(maximum, max(0., budget.remaining() - reserve))
    full_pass = len(state.completed_types) >= type_count
    index_reason = state.index_due_reason(clock(), pages=index_refresh_pages,
        seconds=plan.index_refresh_seconds, full_pass=full_pass)
    if index_reason:
        async def refresh_index(_):
            state.index_last_attempt = clock()
            return await operations.rebuild_index()
        await budget.run("index", refresh_index, cap=database_cap(15))
        if budget.stages["index"]["status"] == "ok":
            state.pages_since_index = 0
            state.apartment_pages_since_index = 0
            state.pending_index_observations = 0
            state.index_dirty_since = None
            state.index_refresh_required = False
            state.index_last_success = clock()
            state.index_failures = 0
            state.index_refreshes += 1
            if full_pass:
                log.info("--- Full rental pass complete ---")
                state.completed_types.clear()
            # A timed database refresh must not multiply external exports.
            # Preserve the previous 3 * 15 durable-page cadence independently
            # of the shorter index deadline, retaining due work on failure.
            if state.pages_since_sheets >= RENTAL_SHEETS_SYNC_PAGES:
                synced = await budget.run("sheets", lambda _: operations.sync_sheets(), cap=database_cap(10))
                if synced is False and budget.stages["sheets"]["status"] == "ok":
                    budget.stages["sheets"]["status"] = "error"
                if synced is True and budget.stages["sheets"]["status"] == "ok":
                    state.pages_since_sheets = 0
                    await operations.set_setting("SHEETS_RENTAL_SYNCED_AT",
                        datetime.now(timezone.utc).isoformat())
        elif budget.stages["index"]["status"] in ("error", "timeout"):
            state.index_failures += 1

    if state.cycle % plan.db_every == 0:
        await budget.run("dedup", lambda _: operations.dedup(), cap=database_cap(8))
        await budget.run("address_bind", lambda _: operations.bind_address("rental_listings"), cap=database_cap(8))
        if plan.allow_geo_complex_guess:
            await budget.run("geo_bind", lambda _: operations.bind_geo(), cap=database_cap(8))

    # Alternate enrichment priority so neither queue monopolizes the budget.
    order = ("backfill", "archive") if state.cycle % 2 else ("archive", "backfill")
    detail_report = {"queue_scope": "apartment", "status": "cycle_stopped" if plan.backfill_batch else "idle"}
    for name in order:
        progress = {}
        if name == "backfill" and plan.backfill_batch:
            # Apartment enrichment cannot be starved by the larger commercial
            # queue. Two monthly turns per daily turn; a cooldown does not
            # consume a turn. Real traffic or a confirmed empty queue advances
            # the persisted turn; an empty monthly queue cannot starve daily.
            period = "daily" if state.detail_turn % 3 == 2 else "monthly"
            result = await budget.run(name, lambda seconds: operations.backfill(
                plan.backfill_batch, budget_seconds=seconds, progress=progress,
                prop_type="apartment", rent_period=period), cap=45)
        elif name == "archive" and plan.archive_batch:
            result = await budget.run(name, lambda seconds: operations.archive(
                limit=plan.archive_batch, budget_seconds=seconds, progress=progress), cap=20)
        else:
            continue
        if not isinstance(result, dict):
            result = progress
        if isinstance(result, dict):
            if name == "backfill":
                detail_report = dict(result)
                detail_report.setdefault("queue_scope", "apartment_daily" if period == "daily" else "apartment")
                detail_report.setdefault("status", "cycle_stopped" if not result else "idle")
                stage_status = budget.stages[name]["status"]
                if stage_status in ("budget_exhausted", "timeout", "error"):
                    detail_report["status"] = ("cycle_stopped" if stage_status == "timeout" else stage_status)
                    if stage_status != "error":
                        detail_report["budget_exhausted"] = True
                empty_queue = stage_status == "ok" and result.get("eligible_due") == 0
                if result.get("requests", 0) or empty_queue:
                    state.detail_turn = (state.detail_turn + 1) % 3
                    await operations.set_setting("RENTAL_DETAIL_TURN", str(state.detail_turn))
                for field in ("checked", "success", "errors", "blocked"):
                    counters[f"detail_{field}"] = result.get(field, 0)
                counters["detail_cooldown_cycles"] = int(bool(result.get("cooldown")))
                counters["detail_budget_exhausted_cycles"] = int(bool(result.get("budget_exhausted")))
            counters[f"{'detail' if name == 'backfill' else 'archive'}_requests"] = result.get("requests", 0)
            counters["blocked"] = counters.get("blocked", 0) + result.get("blocked", 0)
            counters["http_468"] = counters.get("http_468", 0) + result.get("http_468", 0)
            counters["errors"] = counters.get("errors", 0) + result.get("errors", 0)
            if name == "archive":
                counters["archived"] = result.get("archived", 0)
            log.info("Rental %s: %s", name, result)

    for stage in budget.stages.values():
        counters["timeouts"] = counters.get("timeouts", 0) + int(stage["status"] == "timeout")
        counters["errors"] = counters.get("errors", 0) + int(stage["status"] == "error")
    maintenance_seconds = max(0.0, clock() - budget.started)
    counters.update({"list_ms": round(list_seconds * 1000),
                     "maintenance_ms": round(maintenance_seconds * 1000),
                     "sleep_ms": round(sleep_seconds * 1000)})
    cycle = {"at": datetime.now(timezone.utc).isoformat(), "type": prop_type,
             "rent_period": catalog_period(path), "scope": scope_key,
             "cycle": state.cycle, "list_status": list_status,
             "page": step.get("page"), "saved": step.get("saved", 0),
             "head_pages": counters.get("head_pages", 0),
             "durable_pages": counters.get("pages", 0),
             "page_interval_seconds": state.page_intervals.get(scope_key),
             "page_interval_samples": state.interval_samples.get(scope_key, 0),
             "estimated_circle_hours": estimated_circle_hours,
             "traversal_target_hours": 24,
             "traversal_target_status": ("unknown" if estimated_circle_hours is None or list_status != "ok" else
                 "within_target_estimate" if estimated_circle_hours <= 24 else "missed_target_estimate"),
             "list_batch_pages": plan.list_batch_pages,
             "head_refresh_seconds": plan.head_refresh_seconds,
             "detail": detail_report,
             "list_seconds": round(list_seconds, 3), "stages": budget.stages,
             "maintenance_seconds": round(maintenance_seconds, 3),
             "index": {
                 "due_reason": index_reason,
                 "refresh_interval_seconds": plan.index_refresh_seconds,
                 "pending_observations": state.pending_index_observations,
                 "apartment_pages_since_refresh": state.apartment_pages_since_index,
                 "dirty_age_seconds": (round(max(0.0, clock() - state.index_dirty_since), 3)
                     if state.index_dirty_since is not None else None),
                 "last_success_age_seconds": (round(max(0.0, clock() - state.index_last_success), 3)
                     if state.index_last_success is not None else None),
                 "retry_after_seconds": round(state.index_retry_after(clock()), 3),
                 "failures": state.index_failures,
                 "pages_since_sheets": state.pages_since_sheets,
             },
             "geo_complex_guess_enabled": plan.allow_geo_complex_guess,
             "work_seconds": round(max(0.0, clock() - started), 3),
             "sleep_seconds": round(sleep_seconds, 3)}
    try:
        async def persist_metrics():
            await record_rental_cycle(cycle, counters)
            if list_status == "ok" and scope_key in state.page_intervals:
                prefix = cursor_prefix
                await operations.set_setting(f"{prefix}_PAGE_INTERVAL_SEC",
                                             str(state.page_intervals[scope_key]))
                await operations.set_setting(f"{prefix}_PAGE_INTERVAL_SAMPLES",
                                             str(state.interval_samples[scope_key]))
                if estimated_circle_hours is not None:
                    await operations.set_setting(f"{prefix}_ESTIMATED_HOURS", str(estimated_circle_hours))
        await asyncio.wait_for(persist_metrics(), timeout=5)
    except Exception:
        log.warning("Rental cycle metrics could not be persisted", exc_info=True)
    log.info("Rental cycle timing: %s", cycle)
    return cycle


async def bind_rental_geo() -> None:
    # Proximity is a hypothesis, never an observed source complex name/FK.
    return None


async def main() -> None:
    from bot.db.pg import init_pool
    from bot.db import settings
    from bot.core.rental_parser import (
        collect_rental_page, rebuild_rental_index, backfill_rental_details,
        DEFAULT_HEADERS, RENTAL_PATHS, RENTAL_INDEX_REFRESH_PAGES,
    )
    from bot.core.sheets_sync_rental import sync_rental_to_sheets
    from bot.core.dedup_listings import deduplicate_rental_listings
    from bot.core.archive_check import check_archived_rentals
    from bot.core.rebind import bind_by_address
    import httpx

    configure_logging()
    await init_pool(DATABASE_URL)
    revision = service_revision()
    await settings.set("RENTAL_SERVICE_REVISION", revision)
    await settings.set("RENTAL_SERVICE_STARTED_AT", datetime.now(timezone.utc).isoformat())
    log.info("=== Rental service started; revision=%s; list-first maintenance<=60s ===", revision)
    operations = RentalOperations(collect_rental_page, rebuild_rental_index,
        sync_rental_to_sheets, deduplicate_rental_listings, backfill_rental_details,
        check_archived_rentals, bind_by_address, bind_rental_geo, settings.set)
    # Rebuild the legacy index on the first healthy list step after startup.
    # Old aggregates must not remain eligible until the fifteenth new page.
    state = RentalServiceState(index_refresh_required=True)
    await settings.load()
    try:
        state.detail_turn = int(settings.get("RENTAL_DETAIL_TURN", "0")) % 3
    except (ValueError, TypeError):
        state.detail_turn = 0
    paths = list(RENTAL_PATHS.items())
    path_idx = 0
    while True:
        sleep_seconds = random.uniform(60, 180)
        try:
            await settings.load()
            plan = RentalCyclePlan.from_settings(settings.get)
            path, prop_type = paths[path_idx]
            async with httpx.AsyncClient(
                    headers=DEFAULT_HEADERS, follow_redirects=True, timeout=20) as client:
                await run_one_cycle(client, path, prop_type, state, operations, plan,
                    type_count=len(paths), index_refresh_pages=RENTAL_INDEX_REFRESH_PAGES,
                    sleep_seconds=sleep_seconds)
        except Exception:
            log.error("Rental service cycle failed", exc_info=True)
        finally:
            path_idx = (path_idx + 1) % len(paths)
        log.info("Sleeping %.0f sec...", sleep_seconds)
        await asyncio.sleep(sleep_seconds)


if __name__ == "__main__":
    asyncio.run(main())
