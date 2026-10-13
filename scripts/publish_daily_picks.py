#!/usr/bin/env python3
"""Read-only daily selection and atomic public snapshot publisher.

No application pool or migrations are initialized. The connection and
repeatable-read transaction both enforce read-only PostgreSQL access.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.core.daily_picks import (
    LOCAL_TIMEZONE, MAX_PICKS, build_daily_picks, load_daily_picks,
    publish_snapshot, safe_photo_url, snapshot_path,
)

# Restricted input fields. No address, seller contacts, documents, descriptions,
# administrative notes, or application credentials are selected.
CANDIDATES_SQL = """
WITH photos AS (
    SELECT f.listing_id, jsonb_agg(f.photo_url ORDER BY f.id) AS approved_photo_urls
    FROM listing_photo_fingerprints f
    JOIN apartment_listings p ON p.id = f.listing_id
    WHERE f.fetch_status = 'ok' AND f.photo_type = 'interior'
      AND p.is_active IS TRUE AND p.last_seen BETWEEN $1::timestamptz - interval '72 hours' AND $1
      AND p.photos @> jsonb_build_array(f.photo_url)
    GROUP BY f.listing_id
), history AS (
    SELECT listing_id, (array_agg(old_price ORDER BY changed_at))[1] AS first_price_30d,
           (array_agg(new_price ORDER BY changed_at DESC))[1] AS latest_history_price
    FROM price_history
    WHERE changed_at BETWEEN $1::timestamptz - interval '30 days' AND $1
    GROUP BY listing_id
)
SELECT a.id, a.url, a.price, a.area, a.rooms, a.floor, a.floors_total, a.district,
       a.year_built, a.is_owner, a.is_active, a.is_duplicate, a.dup_needs_review,
       a.market_type, a.last_seen, a.resolved_house_id, a.house_attribution,
       a.complex_id, a.complex_resolution, a.lat, a.lon, a.finish_level, pl.property_id,
       c.name AS canonical_complex_name, c.year_built AS complex_year_built,
       p.approved_photo_urls, h.first_price_30d, h.latest_history_price
FROM apartment_listings a
LEFT JOIN property_listings pl ON pl.listing_id = a.id
LEFT JOIN complexes c ON c.id = a.complex_id
LEFT JOIN photos p ON p.listing_id = a.id
LEFT JOIN history h ON h.listing_id = a.id
WHERE a.is_active IS TRUE AND COALESCE(a.is_duplicate, FALSE) = FALSE
  AND COALESCE(a.dup_needs_review, FALSE) = FALSE AND a.market_type = 'secondary'
  AND a.last_seen BETWEEN $1::timestamptz - interval '72 hours' AND $1
  AND a.price > 0 AND a.area > 0
ORDER BY a.id
"""


async def read_candidates(dsn: str, now: datetime) -> list[dict]:
    import asyncpg
    connection = await asyncpg.connect(
        dsn, command_timeout=60,
        server_settings={"default_transaction_read_only": "on", "application_name": "hatuli_daily_picks_readonly"},
    )
    try:
        async with connection.transaction(isolation="repeatable_read", readonly=True):
            return [dict(row) for row in await connection.fetch(CANDIDATES_SQL, now)]
    finally:
        await connection.close()


async def verify_photos(snapshot: dict, deadline: float | None = None) -> tuple[dict, int]:
    """HEAD only: no image download, redirect, parser, or anti-bot bypass."""
    import httpx
    checked = 0
    valid_items = []
    async with httpx.AsyncClient(timeout=3, follow_redirects=False, headers={"User-Agent": "HatuliDailyPicks/1.0"}) as client:
        for item in snapshot["items"]:
            valid = []
            for url in item["photo_urls"]:
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError("Photo verification budget exhausted")
                if safe_photo_url(url) is None:
                    continue
                try:
                    response = await client.head(url)
                    checked += 1
                    if response.status_code == 200 and response.headers.get("content-type", "").lower().startswith("image/"):
                        valid.append(url)
                except httpx.HTTPError:
                    checked += 1
                await asyncio.sleep(.12)
            if valid:
                valid_items.append({**item, "photo_urls": valid})
    snapshot = {**snapshot, "items": valid_items}
    for rank, item in enumerate(valid_items, 1):
        item["rank"] = rank
    return snapshot, checked


@contextmanager
def publisher_lock(path: Path):
    """Nonblocking, process-scoped Linux lock; web workers never acquire it."""
    import fcntl
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + ".lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


async def generate(args, now: datetime) -> dict:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    load_dotenv(ROOT / "krisha_bot" / ".env")
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL is required")
    rows = await read_candidates(dsn, now)
    snapshot = build_daily_picks(rows, now)
    head_count = 0
    if not args.skip_photo_check:
        # A failed image removes its card. Refill from the next eligible item;
        # at most 90 candidates and three photos each, bounded sequential HEADs.
        checked_ids = set()
        attempted_ids = set()
        selected = []
        deadline = time.monotonic() + 240
        for _ in range(3):
            batch = build_daily_picks(rows, now, excluded_ids=attempted_ids)
            attempted_ids.update(item["id"] for item in batch["items"])
            batch, count = await verify_photos(batch, deadline)
            head_count += count
            for item in batch["items"]:
                if item["id"] not in checked_ids:
                    selected.append(item)
                    checked_ids.add(item["id"])
            if len(selected) >= MAX_PICKS:
                break
            # Exclude attempted cards, retaining every row in the comparable pool.
        snapshot["items"] = selected[:MAX_PICKS]
        for rank, item in enumerate(snapshot["items"], 1):
            item["rank"] = rank
        snapshot["source_observed_at"] = max((item["observed_at"] for item in snapshot["items"]), default=now.isoformat())
    return {"snapshot": snapshot, "input_count": len(rows), "photo_heads": head_count}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Read and evaluate; do not publish")
    parser.add_argument("--force", action="store_true", help="Explicitly republish today's snapshot")
    parser.add_argument("--skip-photo-check", action="store_true", help="Dry-run only; avoids CDN HEAD requests")
    parser.add_argument("--allow-partial", action="store_true", help="Explicitly publish fewer than 30 eligible listings")
    args = parser.parse_args()
    if args.skip_photo_check and not args.dry_run:
        parser.error("--skip-photo-check is only permitted with --dry-run")
    now = datetime.now(timezone.utc)
    path = snapshot_path()
    try:
        if args.dry_run:
            result = asyncio.run(generate(args, now))
        else:
            with publisher_lock(path):
                existing = load_daily_picks(path)
                if existing and existing["selection_date"] == now.astimezone(LOCAL_TIMEZONE).date().isoformat() and not args.force:
                    print(json.dumps({"status": "already_published", "selection_date": existing["selection_date"], "count": len(existing["items"])}))
                    return 0
                result = asyncio.run(generate(args, now))
                snapshot = result["snapshot"]
                if len(snapshot["items"]) < MAX_PICKS and not args.allow_partial:
                    raise RuntimeError(f"Only {len(snapshot['items'])} qualifying cards; previous snapshot retained")
                publish_snapshot(snapshot, path)
        snapshot = result["snapshot"]
        print(json.dumps({"status": "dry_run" if args.dry_run else "published", "selection_date": snapshot["selection_date"], "input_count": result["input_count"], "eligible_count": snapshot["total_candidates"], "count": len(snapshot["items"]), "photo_heads": result["photo_heads"], "ids": [item["id"] for item in snapshot["items"]]}, ensure_ascii=False))
        return 0
    except Exception as exc:
        # Do not log DSNs or raw rows; server logs retain only the error class.
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__, "previous_snapshot_retained": True}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
