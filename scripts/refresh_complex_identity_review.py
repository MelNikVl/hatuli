#!/usr/bin/env python3
"""Refresh the small complex review backlog; no identities are modified.

Default is a SELECT-only repeatable-read preview. --publish inserts only new
unconfirmed name-variant hypotheses in complex_duplicate_candidates. Existing
merged/rejected pairs are retained and never reopened. No pool initialization,
migrations, canonical changes, relinking or physical merging occur.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncpg
from dotenv import load_dotenv
from bot.core.complex_identity_review import discover_review_pairs, VERSION


async def refresh(dsn: str, *, limit=20, publish=False):
    conn = await asyncpg.connect(dsn, command_timeout=60)
    try:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            rows = [dict(row) for row in await conn.fetch("""
                SELECT id,name,address,lat,lon,developer_id,parent_complex_id,krisha_url,korter_url,
                    canonical_id,canonical_reason,COALESCE(is_garbage,false) AS is_garbage,
                    COALESCE(is_street,false) AS is_street
                FROM complexes ORDER BY id
            """)]
            counts = {row["complex_id"]: row["n"] for row in await conn.fetch("""
                SELECT complex_id,count(*) AS n FROM apartment_listings
                WHERE complex_id IS NOT NULL GROUP BY 1
            """)}
            existing = [tuple(row) for row in await conn.fetch(
                "SELECT complex_id_a,complex_id_b FROM complex_duplicate_candidates")]
            candidates = discover_review_pairs(rows, counts, existing, limit)
        inserted = []
        if publish:
            async with conn.transaction():
                # A concurrent reviewer/import cannot change the eligible root
                # between the SELECT guard and insertion of a hypothesis.
                await conn.execute("LOCK TABLE complexes IN SHARE MODE")
                for candidate in candidates:
                    evidence = json.dumps(candidate["evidence"], ensure_ascii=False)
                    row = await conn.fetchrow("""
                        INSERT INTO complex_duplicate_candidates(complex_id_a,complex_id_b,
                            translit_key,reason,evidence,method,status)
                        SELECT $1,$2,$3,$4,$5::jsonb,$6,'review'
                        WHERE (SELECT count(*) FROM complexes WHERE id=ANY($7::int[])
                            AND canonical_id IS NULL AND NOT COALESCE(is_garbage,false)
                            AND NOT COALESCE(is_street,false)
                            AND COALESCE(canonical_reason,'')<>'junk_unmatched')=2
                        ON CONFLICT(complex_id_a,complex_id_b) DO NOTHING RETURNING id
                    """, candidate["complex_id_a"],candidate["complex_id_b"],candidate["translit_key"],
                        candidate["reason"],evidence,VERSION,
                        [candidate["complex_id_a"],candidate["complex_id_b"]])
                    if row:
                        inserted.append(row["id"])
        return {"generated_at": datetime.now(timezone.utc).isoformat(),
                "methodology_version": VERSION, "read_only": not publish,
                "identity_writes": 0, "inserted_candidate_ids": inserted,
                "candidates": candidates,
                "notes": ["These are review hypotheses, not confirmed duplicate complexes.",
                          "Phases, buildings and distinct locations require source review.",
                          "Previously merged/rejected pairs are never reopened automatically."]}
    finally:
        await conn.close()


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.limit <= 100:
        parser.error("--limit must be between 1 and 100")
    load_dotenv()
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        parser.error("DATABASE_URL is required")
    report = await refresh(dsn, limit=args.limit, publish=args.publish)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str)+"\n",encoding="utf-8")
    print(json.dumps({"candidates": len(report["candidates"]),
                      "inserted": len(report["inserted_candidate_ids"]), "identity_writes": 0}))


if __name__ == "__main__":
    asyncio.run(main())

