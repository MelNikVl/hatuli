#!/usr/bin/env python3
"""Collect private asking-price observations without database writes.

Code provenance audit: apartment_listings.last_seen is advanced by parsed
sale-card price upserts in service_apartments.py and full_sweep.py. Archive,
reactivation and view-count jobs do not advance it. newbuild_units.last_seen_at
is advanced by price-bearing inventory upserts in newbuild_common.py, while
missing/sold status updates leave it alone. Thus these are source ingestion
timestamps, not the time this collector runs and not confirmed sales.

Current features are captured with known_asof=now. Existing snapshot rows are
never backfilled, and old source observations (>48 hours) are excluded.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.core.price_forecast_observations import publish_capture

QUOTES_SQL = """
SELECT a.id AS listing_id, 'listing'::text AS entity_kind, 'krisha'::text AS source,
       a.last_seen AS source_observed_at, 'active'::text AS source_status,
       TRUE AS source_price_confirmed, 'sale_search_ingestion'::text AS source_provenance,
       a.price,a.area,a.rooms,a.floor,a.floors_total,a.market_type,a.complex_id,
       a.lat,a.lon,NULL::text AS coords_source,
       a.is_duplicate,a.dup_needs_review,p.identity_status AS property_identity_status,
       EXISTS(SELECT 1 FROM listing_archive_history h WHERE h.listing_id=a.id) AS has_prior_reactivation,
       (SELECT max(h.reactivated_at) FROM listing_archive_history h WHERE h.listing_id=a.id) AS latest_reactivated_at
FROM apartment_listings a
LEFT JOIN property_listings pl ON pl.listing_id=a.id
LEFT JOIN properties p ON p.property_id=pl.property_id
WHERE a.is_active IS TRUE AND a.archived_at IS NULL
  AND a.last_seen BETWEEN now()-interval '48 hours' AND now()
  AND NOT coalesce(a.is_duplicate,FALSE) AND NOT coalesce(a.dup_needs_review,FALSE)
  AND a.market_type IN ('primary','secondary') AND a.price>0 AND a.area>0
UNION ALL
SELECT 'newbuild:'||u.id::text AS listing_id, 'unit'::text AS entity_kind,u.source,
       u.last_seen_at AS source_observed_at,'active'::text AS source_status,
       TRUE AS source_price_confirmed,'developer_inventory_ingestion'::text AS source_provenance,
       u.price,u.area,u.rooms,u.floor,u.floors_total,'primary'::text AS market_type,u.complex_id,
       c.lat,c.lon,c.coords_source,
       FALSE AS is_duplicate,FALSE AS dup_needs_review,'source_unit'::text AS property_identity_status,
       FALSE AS has_prior_reactivation,NULL::timestamptz AS latest_reactivated_at
FROM newbuild_units u
LEFT JOIN complexes c ON c.id=u.complex_id
WHERE u.status='available' AND u.last_seen_at BETWEEN now()-interval '48 hours' AND now()
  AND u.price>0 AND u.area>0
ORDER BY listing_id
"""

NODES_SQL = """
SELECT a.id AS listing_id,pl.property_id::text AS property_key,p.address_hash AS trait_key,NULL::text AS photo_key
FROM apartment_listings a
LEFT JOIN property_listings pl ON pl.listing_id=a.id
LEFT JOIN properties p ON p.property_id=pl.property_id
UNION ALL
SELECT f.listing_id,NULL::text AS property_key,NULL::text AS trait_key,f.sha256 AS photo_key
FROM listing_photo_fingerprints f
WHERE f.fetch_status='ok' AND f.photo_type='interior' AND f.sha256 IS NOT NULL
UNION ALL
SELECT 'newbuild:'||u.id::text AS listing_id,NULL::text AS property_key,
       NULL::text AS trait_key,NULL::text AS photo_key
FROM newbuild_units u
"""

EDGES_SQL = """
SELECT a.id AS left_id,a.duplicate_of AS right_id
FROM apartment_listings a WHERE a.duplicate_of IS NOT NULL
UNION
SELECT c.listing_id AS left_id,pl.listing_id AS right_id
FROM property_match_candidates c
JOIN property_listings pl ON pl.property_id=c.candidate_property_id
WHERE c.status='accepted'
UNION
SELECT s.listing_id AS left_id,s.relisted_as AS right_id
FROM sale_outcome_labels s WHERE s.label='relisted' AND s.relisted_as IS NOT NULL
UNION
SELECT l.source_id AS left_id,'newbuild:'||l.unit_id::text AS right_id
FROM unit_source_links l WHERE l.source='krisha'
"""


async def collect_inputs(dsn: str):
    import asyncpg

    connection = await asyncpg.connect(dsn, command_timeout=120,
        server_settings={"default_transaction_read_only": "on", "statement_timeout": "120000"})
    try:
        async with connection.transaction(readonly=True, isolation="repeatable_read"):
            if await connection.fetchval("SHOW transaction_read_only") != "on":
                raise RuntimeError("Observation collector requires a read-only transaction")
            rows = [dict(row) for row in await connection.fetch(QUOTES_SQL)]
            nodes = [dict(row) for row in await connection.fetch(NODES_SQL)]
            edges = [dict(row) for row in await connection.fetch(EDGES_SQL)]
            # Features are known only after this read completes. Do not use a
            # source ingestion timestamp as the feature availability timestamp.
            copied_at = await connection.fetchval("SELECT clock_timestamp()")
            return rows, nodes, edges, copied_at
    finally:
        await connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path(os.environ.get(
        "HATULI_PRICE_OBSERVATION_DIR", str(ROOT / "var" / "price-forecast-observations"))))
    args = parser.parse_args()
    from dotenv import load_dotenv

    env_path = ROOT / ".env"
    load_dotenv(env_path if env_path.exists() else Path.cwd() / ".env")
    rows, nodes, edges, copied_at = asyncio.run(collect_inputs(os.environ["DATABASE_URL"]))
    report = publish_capture(rows, nodes, edges, args.output_dir, captured_at=copied_at)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
