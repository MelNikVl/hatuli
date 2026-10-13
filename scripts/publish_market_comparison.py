#!/usr/bin/env python3
"""Read-only market snapshot publisher. No app pool, migrations or HTTP calls."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.core.market_comparison import build_market_snapshot, publish_market_snapshot, snapshot_path

PRIMARY_SQL = """
SELECT u.id, coalesce(c.canonical_id,c.id) AS complex_id, canonical.name AS complex_name,
       u.source,u.status,u.price,u.area,u.rooms,u.floor,u.floors_total,u.last_seen_at,
       coalesce(c.lat,canonical.lat) AS lat,coalesce(c.lon,canonical.lon) AS lon,
       CASE WHEN c.lat IS NOT NULL AND c.lon IS NOT NULL THEN c.coords_source ELSE canonical.coords_source END AS coords_source,
       coalesce(c.build_status,canonical.build_status) AS build_status,
       coalesce(c.completion_year,canonical.completion_year) AS completion_year,
       coalesce(c.completion_quarter,canonical.completion_quarter) AS completion_quarter,
       coalesce(c.housing_class,canonical.housing_class) AS housing_class
FROM newbuild_units u
JOIN complexes c ON c.id=u.complex_id
JOIN complexes canonical ON canonical.id=coalesce(c.canonical_id,c.id)
WHERE u.status='available' AND u.price>0 AND u.area>0
  AND u.last_seen_at BETWEEN $1::timestamptz-interval '72 hours' AND $1
  AND NOT coalesce(c.is_garbage,false) AND NOT coalesce(c.is_street,false)
  AND NOT coalesce(canonical.is_garbage,false) AND NOT coalesce(canonical.is_street,false)
  AND c.canonical_reason IS DISTINCT FROM 'junk_unmatched'
ORDER BY u.id
"""

SECONDARY_SQL = """
SELECT a.id,a.url,a.market_type,a.price,a.area,a.rooms,a.floor,a.floors_total,
       a.year_built,a.finish_level,a.lat,a.lon,a.is_active,a.is_duplicate,a.dup_needs_review,a.last_seen,
       canonical.id AS complex_id,canonical.name AS complex_name,canonical.housing_class,
       p.property_id,EXISTS(SELECT 1 FROM unit_source_links l WHERE l.source='krisha' AND l.source_id=a.id) AS linked_primary_unit
FROM apartment_listings a
LEFT JOIN complexes c ON c.id=a.complex_id
LEFT JOIN complexes canonical ON canonical.id=coalesce(c.canonical_id,c.id)
  AND NOT coalesce(canonical.is_garbage,false) AND NOT coalesce(canonical.is_street,false)
  AND canonical.canonical_reason IS DISTINCT FROM 'junk_unmatched'
LEFT JOIN property_listings p ON p.listing_id=a.id
WHERE a.market_type='secondary' AND a.is_active IS TRUE
  AND NOT coalesce(a.is_duplicate,false) AND NOT coalesce(a.dup_needs_review,false)
  AND a.last_seen BETWEEN $1::timestamptz-interval '72 hours' AND $1
  AND a.price>0 AND a.area>0
ORDER BY a.id
"""

COVERAGE_SQL = """
SELECT
 (SELECT count(*) FROM apartment_listings WHERE market_type='primary' AND is_active IS TRUE
    AND NOT coalesce(is_duplicate,false) AND NOT coalesce(dup_needs_review,false)
    AND last_seen BETWEEN $1::timestamptz-interval '72 hours' AND $1) AS krisha_primary_recent,
 (SELECT count(*) FROM apartment_listings WHERE market_type='secondary' AND is_active IS TRUE
    AND NOT coalesce(is_duplicate,false) AND NOT coalesce(dup_needs_review,false)
    AND last_seen BETWEEN $1::timestamptz-interval '72 hours' AND $1) AS secondary_recent,
 (SELECT count(*) FROM newbuild_units WHERE status='available' AND last_seen_at BETWEEN $1::timestamptz-interval '72 hours' AND $1) AS primary_available_recent,
 (SELECT count(*) FROM newbuild_units WHERE status='available' AND last_seen_at < $1::timestamptz-interval '72 hours') AS primary_stale_available
"""


async def read_market_rows(dsn: str, now: datetime) -> tuple[list[dict], list[dict], float, dict]:
    import asyncpg
    connection = await asyncpg.connect(
        dsn, command_timeout=60,
        server_settings={"default_transaction_read_only": "on", "application_name": "hatuli_market_comparison_readonly"},
    )
    try:
        async with connection.transaction(isolation="repeatable_read", readonly=True):
            primary = [dict(row) for row in await connection.fetch(PRIMARY_SQL, now)]
            secondary = [dict(row) for row in await connection.fetch(SECONDARY_SQL, now)]
            coverage = dict(await connection.fetchrow(COVERAGE_SQL, now))
            configured_edge = await connection.fetchval("SELECT value FROM app_settings WHERE key='HEX_EDGE_M'")
            edge = float(configured_edge) if configured_edge is not None else 50.0
            return primary, secondary, edge, coverage
    finally:
        await connection.close()


@contextmanager
def publisher_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        if os.name == "posix":
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == "posix":
                fcntl.flock(handle, fcntl.LOCK_UN)


async def run(*, output: Path, edge_m: float | None = None, dry_run: bool = False) -> dict:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    load_dotenv(ROOT / "krisha_bot" / ".env")
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL is required")
    now = datetime.now(timezone.utc)
    primary, secondary, configured_edge, coverage = await read_market_rows(dsn, now)
    snapshot = build_market_snapshot(primary, secondary, now=now, edge_m=edge_m if edge_m is not None else configured_edge, coverage=coverage)
    if not dry_run:
        publish_market_snapshot(snapshot, output)
    return {"generated_at": snapshot["generated_at"], "edge_m": snapshot["edge_m"],
            **snapshot["summary"], "published": not dry_run, "output": str(output)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=snapshot_path(), help="Atomic JSON output (default: HATULI_MARKET_COMPARISON_PATH or var/market-comparison.json)")
    parser.add_argument("--edge-m", type=float, default=None, help="Override scanner edge; default reads HEX_EDGE_M setting (fallback 50 m)")
    parser.add_argument("--dry-run", action="store_true", help="Read-only statistics; do not publish a file")
    args = parser.parse_args()
    try:
        with publisher_lock(Path(f"{args.output}.lock")):
            result = asyncio.run(run(output=args.output, edge_m=args.edge_m, dry_run=args.dry_run))
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        # Never print DB credentials or raw exception text from connection errors.
        print(f"Market snapshot failed: {type(exc).__name__}; previous publication retained", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
