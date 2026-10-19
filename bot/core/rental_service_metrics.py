"""Bounded rental maintenance and atomic daily operational counters.

The existing app_settings table is used: no DDL or database initialization.
Counters describe observations made by the collector, not market-wide totals.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)
COUNTERS = (
    "cycles", "pages", "head_pages", "saved_observations", "raw_cards", "accepted_cards", "unpriced_cards",
    "list_requests", "detail_requests", "archive_requests",
    "list_cooldown_cycles",
    "detail_checked", "detail_success", "detail_errors", "detail_blocked",
    "detail_cooldown_cycles", "detail_budget_exhausted_cycles",
    "new", "updated", "reactivated", "archived", "blocked", "http_468",
    "errors", "timeouts", "list_ms", "maintenance_ms", "sleep_ms",
)


def bounded_number(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    if not math.isfinite(number):
        number = default
    return max(minimum, min(maximum, number))


def rental_list_batch_size(expected_pages: Any, visit_interval_seconds: Any = None,
                           *, maximum: int = 8, target_hours: float = 24.) -> int:
    """Weight durable work by catalog size, within the unchanged request pace.

    Unknown catalog: one discovery batch at the cap. Otherwise the observed
    interval includes other scopes, source/SQL time, maintenance and sleeping.
    The initial 15-minute visit allowance is deliberately conservative. This
    is a scheduling target, never evidence that a pass has been completed.
    """
    maximum = int(bounded_number(maximum, 8, 1, 8))
    pages = bounded_number(expected_pages, 0, 0, 1000000)
    if not pages:
        return maximum
    interval = bounded_number(visit_interval_seconds, 900, 1, 86400)
    hours = bounded_number(target_hours, 24, 1, 24)
    return max(1, min(maximum, math.ceil(pages * interval / (hours * 3600))))


@dataclass(frozen=True)
class RentalCyclePlan:
    """Upper bounds prevent enrichment from consuming the next list step."""
    maintenance_seconds: float = 45.0
    backfill_batch: int = 2
    archive_batch: int = 3
    db_every: int = 5
    allow_geo_complex_guess: bool = False
    index_refresh_seconds: float = 300.0
    list_batch_pages: int = 8
    head_refresh_seconds: float = 2700.0

    @classmethod
    def from_settings(cls, get: Callable[[str, str], Any]) -> "RentalCyclePlan":
        return cls(
            maintenance_seconds=bounded_number(
                get("RENTAL_MAINTENANCE_BUDGET_SEC", "45"), 45, 0, 60),
            backfill_batch=int(bounded_number(
                get("RENTAL_BACKFILL_BATCH", "8"), 2, 0, 2)),
            archive_batch=int(bounded_number(
                get("RENTAL_ARCHIVE_CHECK_BATCH", "60"), 3, 0, 3)),
            db_every=int(bounded_number(
                get("RENTAL_DB_MAINTENANCE_EVERY", "5"), 5, 1, 30)),
            allow_geo_complex_guess=str(get("RENTAL_ALLOW_GEO_COMPLEX_GUESS", "0")).strip() == "1",
            index_refresh_seconds=bounded_number(
                get("RENTAL_INDEX_REFRESH_SEC", "300"), 300, 60, 1800),
            list_batch_pages=int(bounded_number(
                get("RENTAL_LIST_BATCH_PAGES", "8"), 8, 1, 8)),
            head_refresh_seconds=bounded_number(
                get("RENTAL_HEAD_REFRESH_SEC", "2700"), 2700, 1800, 2700),
        )


@dataclass
class MaintenanceBudget:
    seconds: float
    clock: Callable[[], float] = time.monotonic
    started: float = field(init=False)
    stages: dict[str, dict] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.started = self.clock()

    def remaining(self) -> float:
        return max(0.0, self.seconds - (self.clock() - self.started))

    async def run(self, name: str, operation: Callable[[float], Awaitable[Any]],
                  *, cap: float = 30.0) -> Any:
        """Sequential only; cancellation bounds a slow SQL/HTTP operation too."""
        allowance = min(self.remaining(), cap)
        if allowance < 1.0:
            self.stages[name] = {"seconds": 0.0, "status": "budget_exhausted"}
            return None
        started = self.clock()
        status = "ok"
        try:
            return await asyncio.wait_for(operation(allowance), timeout=allowance)
        except asyncio.TimeoutError:
            status = "timeout"
            log.warning("Rental maintenance %s exceeded %.1fs budget", name, allowance)
            return None
        except Exception:
            status = "error"
            log.warning("Rental maintenance %s failed", name, exc_info=True)
            return None
        finally:
            self.stages[name] = {
                "seconds": round(max(0.0, self.clock() - started), 3),
                "status": status,
            }


def source_failure_counters(exc: Exception) -> dict[str, int]:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None) or getattr(exc, "status_code", None)
    blocked = status in (403, 429, 468) or bool(getattr(exc, "source_blocked", False))
    return {"blocked": int(blocked), "http_468": int(status == 468),
            "errors": int(not blocked)}


def daily_counter_payload(increments: dict) -> dict[str, int]:
    """Ignore arbitrary keys and never allow negative/non-finite increments."""
    return {key: int(bounded_number(increments.get(key, 0), 0, 0, 10**12))
            for key in COUNTERS}


async def record_rental_cycle(cycle: dict, increments: dict,
                              *, now: datetime | None = None) -> None:
    from bot.db.pg import execute

    now = now or datetime.now(timezone.utc)
    # Kazakhstan's +05 calendar, matching the production service logs.
    day = now.astimezone(timezone(timedelta(hours=5))).date().isoformat()
    payload = daily_counter_payload(increments)
    await execute("""
        INSERT INTO app_settings (key, value, updated_at) VALUES ($1, $2, now())
        ON CONFLICT (key) DO UPDATE SET value = (
            SELECT jsonb_object_agg(metric.key,
                COALESCE(CASE WHEN (app_settings.value::jsonb ->> metric.key) ~ '^[0-9]+$'
                    THEN (app_settings.value::jsonb ->> metric.key)::bigint END, 0)
                + metric.value::bigint)::text
            FROM jsonb_each_text($2::jsonb) AS metric), updated_at = now()
    """, f"RENTAL_METRICS_V1_{day}", json.dumps(payload, separators=(",", ":")))
    await execute("""
        INSERT INTO app_settings (key, value, updated_at) VALUES ($1, $2, now())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
    """, "RENTAL_SERVICE_CYCLE_LAST", json.dumps(cycle, ensure_ascii=False, separators=(",", ":")))
