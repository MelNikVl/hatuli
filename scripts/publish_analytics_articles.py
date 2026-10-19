#!/usr/bin/env python3
"""Export a private read-only digest, or publish an approved daily article pair.

No application pool, migrations, model API, scraping, or news fetch is started.
Official facts can be supplied as a reviewed digest without connecting to any
database. Observation exports are deliberately not cleared for republication.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.core.analytics_articles import (
    LOCAL_TIMEZONE, MAX_FILE_BYTES, MAX_SCAN, build_digest, build_edition,
    load_article, publish_edition, validate_digest, normalize_local_edition,
    build_revision, publish_revision,
)

SALES_SQL = """
SELECT a.id,a.url,a.price,a.area,a.rooms,a.district,a.market_type,a.last_seen,
       a.is_active,a.archived_at,a.is_duplicate,a.dup_needs_review,a.duplicate_of,
       p.property_id,
       (coalesce(p.identity_count,0)>1 OR EXISTS (
          SELECT 1 FROM property_match_candidates review
          WHERE review.listing_id=a.id AND review.status='pending'
       )) AS property_review_pending
FROM apartment_listings a
LEFT JOIN LATERAL (
    SELECT min(property_id) AS property_id,count(DISTINCT property_id) AS identity_count
    FROM property_listings WHERE listing_id=a.id
) p ON TRUE
WHERE a.is_active IS TRUE AND a.archived_at IS NULL AND a.market_type='secondary'
  AND NOT coalesce(a.is_duplicate,FALSE) AND NOT coalesce(a.dup_needs_review,FALSE)
  AND nullif(btrim(a.duplicate_of),'') IS NULL
  AND a.last_seen BETWEEN $1::timestamptz-interval '72 hours' AND $1
ORDER BY a.id LIMIT $2
"""

RENTAL_SQL = """
SELECT r.id,r.url,r.title,r.price,r.area,r.rooms,r.district,r.city,r.prop_type,
       r.rent_period,r.rent_period_conflict,r.is_active,r.archived_at,
       r.is_duplicate,r.dup_needs_review,r.duplicate_of,r.last_seen,
       r.rental_episode_started_at,r.rental_episode_last_seen_at
FROM rental_listings r
WHERE r.city='astana' AND r.prop_type='apartment' AND r.is_active IS TRUE
  AND r.archived_at IS NULL AND r.rent_period IN ('monthly','daily')
  AND NOT coalesce(r.rent_period_conflict,FALSE) AND NOT coalesce(r.is_duplicate,FALSE)
  AND NOT coalesce(r.dup_needs_review,FALSE) AND nullif(btrim(r.duplicate_of),'') IS NULL
  AND r.last_seen BETWEEN $1::timestamptz-interval '72 hours' AND $1
ORDER BY r.id LIMIT $2
"""


async def read_digest(dsn, now):
    import asyncpg
    connection = await asyncpg.connect(
        dsn, command_timeout=60,
        server_settings={"default_transaction_read_only": "on", "application_name": "hatuli_articles_digest_readonly"},
    )
    try:
        async with connection.transaction(readonly=True, isolation="repeatable_read"):
            if await connection.fetchval("SHOW transaction_read_only") != "on":
                raise RuntimeError("A read-only transaction is required")
            if await connection.fetchval("SHOW transaction_isolation") != "repeatable read":
                raise RuntimeError("A repeatable-read snapshot is required")
            sales = [dict(row) for row in await connection.fetch(SALES_SQL, now, MAX_SCAN + 1)]
            rentals = [dict(row) for row in await connection.fetch(RENTAL_SQL, now, MAX_SCAN + 1)]
            if len(sales) > MAX_SCAN or len(rentals) > MAX_SCAN:
                raise ValueError("The bounded scan is incomplete; refusing truncated statistics")
            return build_digest(sales, rentals, now=now)
    finally:
        await connection.close()


def read_input(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("Invalid editorial input file")
    with path.open(encoding="utf-8-sig") as handle:
        return json.load(handle)


def write_private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("w", encoding="utf-8") as handle:
        os.chmod(path, 0o600)
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="Project configuration/archive directory")
    parser.add_argument("--export-digest", type=Path, help="Private observation aggregates; no publication rights implied")
    parser.add_argument("--articles", type=Path, help="Reviewed editorial pair with dated fact references")
    parser.add_argument("--digest", type=Path, help="Reviewed aggregate digest, including official publications")
    parser.add_argument("--check", "--check-only", action="store_true", help="Validate the reviewed pair without writing files or connecting to DB")
    parser.add_argument("--local", action="store_true", help="Publish a separate immutable pair of reviewed local research; retain the original daily edition")
    parser.add_argument("--revise", action="store_true", help="Explicitly revise a published daily pair through an immutable overlay; run --check-only first")
    parser.add_argument("--base-sha256", help="Required with --revise: SHA256 of the unchanged original daily archive")
    parser.add_argument("--reason", help="Required with --revise: plain editorial correction reason, without URLs or HTML")
    args = parser.parse_args(argv)
    if args.export_digest and (args.articles or args.digest or args.check or args.local or args.revise or args.base_sha256 or args.reason) or not args.export_digest and not (args.articles and args.digest):
        parser.error("Choose --export-digest PATH or both --articles PATH --digest PATH")
    if args.revise and (args.local or not args.base_sha256 or not args.reason):
        parser.error("--revise requires --base-sha256 and --reason; separate local research is not revised")
    if not args.revise and (args.base_sha256 or args.reason):
        parser.error("--base-sha256 and --reason are only valid with --revise")
    now = datetime.now(timezone.utc)
    day = now.astimezone(LOCAL_TIMEZONE).date().isoformat()
    if args.export_digest:
        from dotenv import dotenv_values
        dsn = os.environ.get("DATABASE_URL") or dotenv_values(args.root / ".env", interpolate=False).get("DATABASE_URL")
        if not dsn:
            parser.error("DATABASE_URL required for the private read-only export")
        digest = asyncio.run(read_digest(dsn, now))
        write_private(args.export_digest, digest)
        print(json.dumps({"exported_facts": len(digest["facts"]), "database_writes": 0,
                          "publication_rights": "not_cleared"}))
        return 0
    # No DB/environment loading in the publication branch. A complete existing
    # edition is immutable and idempotent, even when new input paths disappear.
    if args.revise:
        digest = validate_digest(read_input(args.digest), published_at=now)
        revision = build_revision(read_input(args.articles), digest, root=args.root,
                                  base_sha256=args.base_sha256, reason=args.reason, now=now)
        if args.check:
            print(json.dumps({"status": "revision_validated", "edition_date": revision["edition_date"],
                              "articles": 2, "base_sha256": args.base_sha256,
                              "modified_at": revision["modified_at"], "file_writes": 0, "database_writes": 0}))
            return 0
        result = publish_revision(revision, root=args.root, now=now)
        print(json.dumps({**result, "database_writes": 0}))
        return 0
    suffix = "-local" if args.local else ""
    if not args.check and all(load_article(f"{day}-{kind}{suffix}", root=args.root, now=now) for kind in ("market", "rent")):
        print(json.dumps({"status": "already_published", "edition_date": day, "articles": 2, "database_writes": 0}))
        return 0
    digest = validate_digest(read_input(args.digest), published_at=now)
    edition = build_edition(read_input(args.articles), digest, now=now)
    if args.local:
        normalize_local_edition(edition, now=now)
    if args.check:
        print(json.dumps({"status": "validated", "edition_date": day, "articles": 2, "file_writes": 0, "database_writes": 0}))
        return 0
    result = publish_edition(edition, root=args.root, now=now, local=args.local)
    print(json.dumps({**result, "database_writes": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
