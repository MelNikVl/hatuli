#!/usr/bin/env python3
"""Audit global-photo boilerplate and reaggregate only a frozen affected set.

Default mode is read-only.  With --out it writes a manifest containing the
affected_candidate_ids.  --apply deliberately requires that manifest and
reaggregates with saved fingerprints only: no CDN download, no SigLIP run,
no candidate-status or physical-merge change.
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


def load_candidate_ids(path: str) -> list[int]:
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    values = raw.get("affected_candidate_ids") if isinstance(raw, dict) else raw
    if not isinstance(values, list):
        raise ValueError("manifest must be a JSON list or contain affected_candidate_ids")
    ids: list[int] = []
    for value in values:
        if isinstance(value, bool):
            raise ValueError("candidate IDs must be positive integers")
        try:
            candidate_id = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("candidate IDs must be positive integers") from exc
        if candidate_id <= 0:
            raise ValueError("candidate IDs must be positive integers")
        if candidate_id not in ids:
            ids.append(candidate_id)
    if not ids:
        raise ValueError("manifest contains no candidate IDs")
    return ids


async def audit() -> dict:
    from bot.db.pg import fetch
    from bot.identity.photo_evidence import _global_boilerplate_keys, is_global_boilerplate_fingerprint

    evidence_rows = await fetch(
        "SELECT candidate_id, matched_photos FROM property_candidate_photo_evidence "
        "WHERE jsonb_array_length(COALESCE(matched_photos, '[]'::jsonb)) > 0"
    )
    urls_by_candidate: dict[int, set[str]] = {}
    for row in evidence_rows:
        matches = row["matched_photos"]
        if isinstance(matches, str):
            matches = json.loads(matches)
        urls = {
            url for match in (matches or []) for url in (match.get("a_url"), match.get("b_url"))
            if isinstance(url, str)
        }
        if urls:
            urls_by_candidate[row["candidate_id"]] = urls

    all_urls = sorted({url for urls in urls_by_candidate.values() for url in urls})
    fingerprints = []
    if all_urls:
        fingerprints = [dict(row) for row in await fetch(
            "SELECT DISTINCT photo_url, sha256, phash FROM listing_photo_fingerprints "
            "WHERE photo_url = ANY($1::text[])", all_urls
        )]
    frequent_sha256, frequent_phash = await _global_boilerplate_keys(fingerprints)
    boilerplate_urls = {
        fp["photo_url"] for fp in fingerprints
        if is_global_boilerplate_fingerprint(
            fp, frequent_sha256=frequent_sha256, frequent_phash=frequent_phash
        )
    }
    affected = sorted(
        candidate_id for candidate_id, urls in urls_by_candidate.items() if urls & boilerplate_urls
    )
    return {
        "evidence_candidates_scanned": len(evidence_rows),
        "matched_photo_urls_scanned": len(all_urls),
        "global_boilerplate_photo_urls": len(boilerplate_urls),
        "affected_candidate_ids": affected,
    }


async def reaggregate(candidate_ids: list[int]) -> dict:
    from bot.identity.photo_evidence import aggregate_candidate_evidence
    from bot.identity.property_linker import recompute_corroborating_methods

    for candidate_id in candidate_ids:
        await aggregate_candidate_evidence(candidate_id, reuse_existing_fingerprints=True)
        await recompute_corroborating_methods(candidate_id)
    return {"reaggregated_candidates": len(candidate_ids), "candidate_ids": candidate_ids}


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", help="write read-only audit manifest to this JSON file")
    ap.add_argument("--apply", action="store_true", help="reaggregate IDs from a frozen manifest")
    ap.add_argument("--candidate-ids-file", help="manifest from a prior audit")
    args = ap.parse_args()
    if args.apply and not args.candidate_ids_file:
        ap.error("--apply requires --candidate-ids-file from a prior audit")

    from bot.db.pg import close_pool, init_pool
    await init_pool(DATABASE_URL)
    try:
        report = await reaggregate(load_candidate_ids(args.candidate_ids_file)) if args.apply else await audit()
    finally:
        await close_pool()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    asyncio.run(main())
