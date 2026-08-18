#!/usr/bin/env python3
"""Remove known advertisement images from working listing photos and refresh
only affected photo evidence. Raw cache and fingerprint rows are retained for
audit. Run with --apply only after inspecting the default dry-run report.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")


async def _affected_candidate_ids() -> list[int]:
    from bot.db.pg import fetch
    from bot.identity.photo_evidence import BLOCKED_PHOTO_PHASH, BLOCKED_PHOTO_SHA256

    rows = await fetch(
        """
        SELECT DISTINCT pcpe.candidate_id
        FROM property_candidate_photo_evidence pcpe
        JOIN property_match_candidates pmc ON pmc.candidate_id = pcpe.candidate_id
        JOIN LATERAL (
            SELECT pl.listing_id
            FROM property_listings pl JOIN apartment_listings al ON al.id = pl.listing_id
            WHERE pl.property_id = pmc.candidate_property_id
            ORDER BY al.last_seen DESC NULLS LAST, al.id LIMIT 1
        ) side_b ON true
        JOIN LATERAL jsonb_array_elements(pcpe.matched_photos) m ON true
        WHERE EXISTS (
            SELECT 1 FROM listing_photo_fingerprints f
            WHERE f.listing_id = pmc.listing_id AND f.photo_url = m->>'a_url'
              AND (f.sha256 = ANY($1::text[]) OR f.phash = ANY($2::text[]))
        ) OR EXISTS (
            SELECT 1 FROM listing_photo_fingerprints f
            WHERE f.listing_id = side_b.listing_id AND f.photo_url = m->>'b_url'
              AND (f.sha256 = ANY($1::text[]) OR f.phash = ANY($2::text[]))
        )
        """,
        list(BLOCKED_PHOTO_SHA256), list(BLOCKED_PHOTO_PHASH),
    )
    return [r["candidate_id"] for r in rows]


async def run(apply: bool) -> dict:
    from bot.db.pg import execute, fetchval
    from bot.identity.photo_evidence import (
        BLOCKED_PHOTO_PHASH, BLOCKED_PHOTO_SHA256, aggregate_candidate_evidence,
    )
    from bot.identity.property_linker import recompute_corroborating_methods

    fingerprint_rows = await fetchval(
        "SELECT count(*) FROM listing_photo_fingerprints "
        "WHERE sha256 = ANY($1::text[]) OR phash = ANY($2::text[])",
        list(BLOCKED_PHOTO_SHA256), list(BLOCKED_PHOTO_PHASH),
    )
    working_photo_rows = await fetchval(
        """SELECT count(*) FROM apartment_listings al JOIN listing_photo_fingerprints f
               ON f.listing_id=al.id AND al.photos ? f.photo_url
            WHERE f.sha256 = ANY($1::text[]) OR f.phash = ANY($2::text[])""",
        list(BLOCKED_PHOTO_SHA256), list(BLOCKED_PHOTO_PHASH),
    )
    candidate_ids = await _affected_candidate_ids()
    report = {"apply": apply, "fingerprints": fingerprint_rows,
              "working_photo_urls": working_photo_rows,
              "affected_evidence_candidates": len(candidate_ids)}
    if not apply:
        return report

    await execute(
        """INSERT INTO blocked_photo_urls (url, reason)
           SELECT DISTINCT photo_url, 'known_ad_fingerprint'
           FROM listing_photo_fingerprints
           WHERE sha256 = ANY($1::text[]) OR phash = ANY($2::text[])
           ON CONFLICT (url) DO UPDATE SET reason = EXCLUDED.reason""",
        list(BLOCKED_PHOTO_SHA256), list(BLOCKED_PHOTO_PHASH),
    )
    await execute(
        """UPDATE apartment_listings al SET photos = COALESCE((
               SELECT jsonb_agg(p)
               FROM jsonb_array_elements_text(al.photos) p
               WHERE NOT EXISTS (
                   SELECT 1 FROM listing_photo_fingerprints f
                   WHERE f.listing_id=al.id AND f.photo_url=p
                     AND (f.sha256 = ANY($1::text[]) OR f.phash = ANY($2::text[]))
               )), '[]'::jsonb)
           WHERE EXISTS (
               SELECT 1 FROM listing_photo_fingerprints f
               WHERE f.listing_id=al.id AND al.photos ? f.photo_url
                 AND (f.sha256 = ANY($1::text[]) OR f.phash = ANY($2::text[]))
           )""",
        list(BLOCKED_PHOTO_SHA256), list(BLOCKED_PHOTO_PHASH),
    )
    for candidate_id in candidate_ids:
        await aggregate_candidate_evidence(candidate_id, reuse_existing_fingerprints=True)
        await recompute_corroborating_methods(candidate_id)
    return report


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="perform targeted cleanup and reaggregation")
    args = ap.parse_args()
    from bot.db.pg import close_pool, init_pool
    await init_pool(DATABASE_URL)
    try:
        print(json.dumps(await run(args.apply), ensure_ascii=False))
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
