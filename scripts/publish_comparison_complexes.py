#!/usr/bin/env python3
"""Publish the complete eligible budget-selector inventory, PostgreSQL read-only."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.core.comparison_complexes import (
    MAX_ROWS, build_comparison_complexes, complex_catalog_path, publish_comparison_complexes,
)

READS = (
    ("complexes", """SELECT id,name,address,lat,lon,is_garbage,is_street,is_umbrella,parent_complex_id,
        canonical_id,canonical_reason,geo_quarantined_at,krisha_url,korter_url,
        source_info->'homsters'->>'url' AS homsters_url,source_info->'korter'->>'url' AS source_korter_url
        FROM complexes ORDER BY id LIMIT $1""", MAX_ROWS),
    ("aliases", "SELECT complex_id,name,status FROM complex_aliases WHERE status='verified' ORDER BY alias_id LIMIT $1", 100_000),
    ("addresses", "SELECT complex_id,city,status FROM complex_addresses WHERE status='verified' ORDER BY address_id LIMIT $1", 100_000),
    ("links", "SELECT complex_id,url FROM complex_source_links ORDER BY id LIMIT $1", 100_000),
    ("reviews", "SELECT complex_id_a,complex_id_b FROM complex_duplicate_candidates WHERE status='review' ORDER BY id LIMIT $1", 100_000),
    ("relations", "SELECT complex_id_a,complex_id_b,relation_type FROM complex_relations ORDER BY relation_id LIMIT $1", 100_000),
    ("conflicts", "SELECT complex_id,conflict_with_complex_id FROM complex_source_link_candidates WHERE kind='conflict' ORDER BY id LIMIT $1", 100_000),
)


async def read_catalog(dsn):
    import asyncpg
    connection = await asyncpg.connect(dsn, command_timeout=30, server_settings={
        "default_transaction_read_only": "on", "application_name": "hatuli_comparison_complexes_readonly"})
    try:
        async with connection.transaction(isolation="repeatable_read", readonly=True):
            await connection.execute("SET LOCAL statement_timeout = '20s'")
            result = {}
            for name, query, maximum in READS:
                rows = [dict(row) for row in await connection.fetch(query, maximum + 1)]
                if len(rows) > maximum:
                    raise ValueError("Incomplete bounded catalog scan")
                result[name] = rows
            return result
    finally:
        await connection.close()


@contextmanager
def publisher_lock(path):
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


async def run(*, output, dry_run=False):
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL is required")
    rows = await read_catalog(dsn)
    # Timestamp only after the consistent reads completed successfully.
    now = datetime.now(timezone.utc)
    data = build_comparison_complexes(**rows, now=now)
    if not dry_run:
        publish_comparison_complexes(data, output, now=now)
    return {"generated_at": data["generated_at"], "count": data["count"], "coverage": data["coverage"],
            "published": not dry_run, "output": str(output)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=complex_catalog_path())
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        with publisher_lock(Path(str(args.output) + ".lock")):
            result = asyncio.run(run(output=args.output, dry_run=args.dry_run))
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        # Database connection errors can contain credentials: don't echo them.
        print(f"Complex catalog failed: {type(exc).__name__}; previous publication retained", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
