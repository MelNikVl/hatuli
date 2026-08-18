#!/usr/bin/env python3
"""Build a deterministic, read-only manifest for one bounded photo batch.

The manifest binds candidate IDs and both listing sides together. It is the
shared input for phase 1 fingerprinting, scoped AI classification, and offline
reaggregation; it does not alter candidates, evidence, or listings.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")


_PRIORITY_SQL = """
    SELECT pmc.candidate_id, pmc.listing_id AS listing_id_a, side_b.listing_id AS listing_id_b,
           pmc.match_method, pmc.match_score
    FROM property_match_candidates pmc
    JOIN LATERAL (
        SELECT pl.listing_id
        FROM property_listings pl JOIN apartment_listings al ON al.id = pl.listing_id
        WHERE pl.property_id = pmc.candidate_property_id
        ORDER BY al.last_seen DESC NULLS LAST, al.id LIMIT 1
    ) side_b ON true
    WHERE pmc.status = 'pending'
      AND NOT EXISTS (
          SELECT 1 FROM property_candidate_photo_evidence pcpe
          WHERE pcpe.candidate_id = pmc.candidate_id AND pcpe.processing_status = 'ok'
      )
    ORDER BY
        CASE WHEN jsonb_array_length(COALESCE(pmc.evidence->'corroborating_methods', '[]'::jsonb)) >= 2 THEN 0 ELSE 1 END,
        CASE pmc.match_method WHEN 'exact_hash' THEN 0 WHEN 'dedup_listings' THEN 1 WHEN 'fuzzy' THEN 2 ELSE 3 END,
        pmc.match_score DESC, pmc.candidate_id ASC
    LIMIT $1
"""


def build_manifest(rows: list[dict]) -> dict:
    listing_ids = list(dict.fromkeys(
        listing_id for row in rows for listing_id in (row["listing_id_a"], row["listing_id_b"])
    ))
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "selection": "pending candidates without ok photo evidence; deterministic priority order",
        "candidate_ids": [row["candidate_id"] for row in rows],
        "listing_ids": listing_ids,
        "rows": rows,
    }


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--out", default="photo_evidence_batch_manifest.json")
    ap.add_argument("--dry-run", action="store_true", help="print manifest without writing a file")
    args = ap.parse_args()
    if args.limit <= 0:
        raise ValueError("--limit must be positive")

    from bot.db.pg import close_pool, fetch, init_pool
    await init_pool(DATABASE_URL)
    try:
        rows = [dict(row) for row in await fetch(_PRIORITY_SQL, args.limit)]
    finally:
        await close_pool()
    manifest = build_manifest(rows)
    print(json.dumps({k: v for k, v in manifest.items() if k != "rows"}, ensure_ascii=False, indent=2, default=str))
    if not args.dry_run:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2, default=str)


if __name__ == "__main__":
    asyncio.run(main())
