#!/usr/bin/env python3
"""Aggregate-only, strictly read-only audit of forecast data readiness.

No application pool, migrations, model training, or contact/address fields.
Daily database copies are not counted as independent source observations.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

QUERIES = {
    "listings": """
        SELECT count(*) AS rows, min(first_seen) AS first_seen_min,
               max(last_seen) AS last_seen_max,
               count(*) FILTER (WHERE is_active IS TRUE) AS active,
               count(*) FILTER (WHERE is_active IS TRUE AND last_seen >= now()-interval '72 hours') AS fresh_active,
               count(*) FILTER (WHERE coalesce(is_duplicate,false)) AS duplicates,
               count(*) FILTER (WHERE coalesce(dup_needs_review,false)) AS pending_duplicate_review
        FROM apartment_listings
    """,
    "fresh_population": """
        SELECT market_type, count(*) AS listings,
               count(DISTINCT pl.property_id) AS linked_properties,
               count(*) FILTER (WHERE pl.property_id IS NULL) AS unlinked_listings,
               count(*) FILTER (WHERE a.resolved_house_id IS NOT NULL) AS resolved_house,
               count(*) FILTER (WHERE a.complex_id IS NOT NULL) AS resolved_complex
        FROM apartment_listings a
        LEFT JOIN property_listings pl ON pl.listing_id=a.id
        WHERE a.is_active IS TRUE AND a.last_seen >= now()-interval '72 hours'
          AND NOT coalesce(a.is_duplicate,false) AND NOT coalesce(a.dup_needs_review,false)
          AND a.price > 0 AND a.area > 0
        GROUP BY market_type ORDER BY market_type
    """,
    "price_history": """
        SELECT count(*) AS events, count(DISTINCT listing_id) AS listings,
               min(changed_at) AS first_at, max(changed_at) AS last_at,
               count(*) FILTER (WHERE new_price < old_price) AS cuts,
               count(*) FILTER (WHERE new_price > old_price) AS raises,
               count(*) FILTER (WHERE new_price IS NULL OR old_price IS NULL OR new_price<=0 OR old_price<=0) AS invalid
        FROM price_history
    """,
    "price_history_months": """
        SELECT date_trunc('month',changed_at)::date AS month, count(*) AS events,
               count(DISTINCT listing_id) AS listings
        FROM price_history GROUP BY 1 ORDER BY 1
    """,
    "price_history_integrity": """
        WITH p AS (
            SELECT listing_id,old_price,new_price,changed_at,
                   lag(new_price) OVER(PARTITION BY listing_id ORDER BY changed_at) AS previous_new
            FROM price_history
        )
        SELECT count(*) AS events,
               count(*) FILTER(WHERE previous_new IS NOT NULL AND old_price<>previous_new) AS broken_chain_events,
               count(DISTINCT listing_id) FILTER(WHERE previous_new IS NOT NULL AND old_price<>previous_new) AS broken_chain_listings,
               count(*) FILTER(WHERE abs(new_price::float/old_price-1)>0.25) AS over25pct_events,
               count(*) FILTER(WHERE abs(new_price::float/old_price-1)>0.5) AS over50pct_events,
               percentile_cont(0.5) WITHIN GROUP(ORDER BY abs(new_price::float/old_price-1)) AS median_absolute_change
        FROM p WHERE old_price>0
    """,
    "listing_snapshots": """
        SELECT count(*) AS rows, count(DISTINCT listing_id) AS listings,
               count(DISTINCT date) AS days, min(date) AS first_day, max(date) AS last_day,
               count(*) FILTER (WHERE price > 0) AS priced_rows
        FROM listing_snapshots
    """,
    "snapshot_dates": """
        SELECT date, count(*) AS rows, count(*) FILTER(WHERE is_active IS TRUE) AS active_copies,
               count(*) FILTER(WHERE views IS NOT NULL) AS views_copies
        FROM listing_snapshots GROUP BY date ORDER BY date
    """,
    "property_identity": """
        WITH p AS (SELECT property_id,count(*) AS n FROM property_listings GROUP BY property_id)
        SELECT count(*) AS properties, sum(n) AS linked_listings,
               count(*) FILTER (WHERE n>1) AS multi_listing_properties, max(n) AS max_listings_per_property
        FROM p
    """,
    "property_links": """
        SELECT link_method, count(*) AS rows, min(linked_at) AS first_at,
               max(linked_at) AS last_at, min(confidence) AS min_confidence
        FROM property_listings GROUP BY link_method ORDER BY link_method
    """,
    "property_traits_observed": """
        SELECT identity_status,count(*) AS properties,min(created_at) AS first_created,max(created_at) AS last_created,
               count(*) FILTER(WHERE created_at < '2026-08-22'::timestamptz) AS before_aug22,
               count(*) FILTER(WHERE created_at < '2026-09-01'::timestamptz) AS before_sep01,
               count(*) FILTER(WHERE created_at < '2026-09-15'::timestamptz) AS before_sep15,
               count(*) FILTER(WHERE created_at < '2026-09-28'::timestamptz) AS before_sep28,
               percentile_cont(0.5) WITHIN GROUP(ORDER BY extract(epoch FROM created_at)) AS created_median_epoch
        FROM properties GROUP BY identity_status ORDER BY identity_status
    """,
    "property_link_observed": """
        SELECT count(*) AS links,min(linked_at) AS first_at,max(linked_at) AS last_at,
               count(*) FILTER(WHERE linked_at < '2026-08-22'::timestamptz) AS before_aug22,
               count(*) FILTER(WHERE linked_at < '2026-09-01'::timestamptz) AS before_sep01,
               count(*) FILTER(WHERE linked_at < '2026-09-15'::timestamptz) AS before_sep15,
               count(*) FILTER(WHERE linked_at < '2026-09-28'::timestamptz) AS before_sep28,
               percentile_cont(0.5) WITHIN GROUP(ORDER BY extract(epoch FROM linked_at)) AS linked_median_epoch
        FROM property_listings
    """,
    "property_trait_drift": """
        SELECT count(*) AS linked_listings,
               count(*) FILTER(WHERE a.rooms IS DISTINCT FROM p.rooms) AS rooms_differ,
               count(*) FILTER(WHERE a.area IS DISTINCT FROM p.area_sqm) AS area_differ,
               count(*) FILTER(WHERE a.floor IS DISTINCT FROM p.floor) AS floor_differ,
               count(*) FILTER(WHERE a.rooms IS DISTINCT FROM p.rooms OR a.area IS DISTINCT FROM p.area_sqm
                 OR a.floor IS DISTINCT FROM p.floor) AS any_trait_differ
        FROM apartment_listings a JOIN property_listings pl ON pl.listing_id=a.id
        JOIN properties p ON p.property_id=pl.property_id
    """,
    "dedup_markings": """
        SELECT dup_match,count(*) AS listings,
               count(*) FILTER(WHERE duplicate_of IS NOT NULL) AS has_duplicate_target,
               count(*) FILTER(WHERE NOT coalesce(is_duplicate,false)) AS not_marked_duplicate
        FROM apartment_listings GROUP BY dup_match ORDER BY dup_match
    """,
    "photo_fingerprints": """
        SELECT fetch_status,photo_type,count(*) AS photos,count(DISTINCT listing_id) AS listings,
               min(computed_at) AS first_computed,max(computed_at) AS last_computed
        FROM listing_photo_fingerprints GROUP BY fetch_status,photo_type ORDER BY fetch_status,photo_type
    """,
    "exact_interior_photo_groups": """
        WITH p AS (
            SELECT sha256,count(DISTINCT listing_id) AS n
            FROM listing_photo_fingerprints
            WHERE fetch_status='ok' AND photo_type='interior' AND sha256 IS NOT NULL GROUP BY sha256
        )
        SELECT count(*) AS hashes,count(*) FILTER(WHERE n>1) AS shared_hashes,
               sum(n) FILTER(WHERE n>1) AS listing_votes_in_shared_hashes,max(n) AS max_shared_listings
        FROM p
    """,
    "match_candidates": """
        SELECT status,match_method,count(*) AS pairs,count(DISTINCT listing_id) AS listings
        FROM property_match_candidates GROUP BY status,match_method ORDER BY status,match_method
    """,
    "photo_match_evidence": """
        SELECT processing_status,count(*) AS pairs,
               count(*) FILTER(WHERE shared_unit_specific_count>0) AS unit_specific_pairs,
               count(*) FILTER(WHERE exact_shared_count>0) AS exact_pairs,
               count(*) FILTER(WHERE shared_unit_specific_count>=2) AS two_unit_specific_pairs,
               min(computed_at) AS first_at,max(computed_at) AS last_at
        FROM property_candidate_photo_evidence GROUP BY processing_status ORDER BY processing_status
    """,
    "archive_history": """
        SELECT count(*) AS reactivation_events, count(DISTINCT listing_id) AS reactivated_listings,
               min(archived_at) AS first_at, max(reactivated_at) AS last_at
        FROM listing_archive_history
    """,
    "sale_labels": """
        SELECT label,source,count(*) AS rows FROM sale_outcome_labels GROUP BY label,source ORDER BY label,source
    """,
    "liquidity_predictions": """
        SELECT count(*) AS rows,count(DISTINCT listing_id) AS listings,
               min(as_of) AS first_day,max(as_of) AS last_day,count(DISTINCT as_of) AS days
        FROM liquidity_predictions
    """,
    "deal_score_snapshots": """
        SELECT count(*) AS rows,count(DISTINCT listing_id) AS listings,
               min(observed_at) AS first_at,max(observed_at) AS last_at,
               count(DISTINCT observed_at::date) AS days
        FROM deal_score_snapshots
    """,
    "newbuild_price_history": """
        SELECT count(*) AS rows,count(DISTINCT unit_id) AS units,
               min(changed_at) AS first_at,max(changed_at) AS last_at
        FROM newbuild_unit_price_history
    """,
    "newbuild_history_spans": """
        WITH u AS (
            SELECT unit_id,count(*) AS n,min(changed_at) AS first_at,max(changed_at) AS last_at
            FROM newbuild_unit_price_history GROUP BY unit_id
        )
        SELECT count(*) AS units,count(*) FILTER(WHERE n>1) AS multi_observation_units,
               count(*) FILTER(WHERE last_at-first_at >= interval '14 days') AS span14_units,
               count(*) FILTER(WHERE last_at-first_at >= interval '30 days') AS span30_units,
               count(*) FILTER(WHERE last_at-first_at >= interval '60 days') AS span60_units
        FROM u
    """,
    "newbuild_sources": """
        SELECT source,status,count(*) AS units,min(first_seen_at) AS first_at,max(last_seen_at) AS last_at
        FROM newbuild_units GROUP BY source,status ORDER BY source,status
    """,
    "price_history_spans": """
        WITH p AS (
            SELECT listing_id,count(*) AS n,min(changed_at) AS first_at,max(changed_at) AS last_at
            FROM price_history GROUP BY listing_id
        )
        SELECT count(*) AS listings,count(*) FILTER(WHERE n>1) AS multi_change_listings,
               count(*) FILTER(WHERE last_at-first_at >= interval '14 days') AS change_span14_listings,
               count(*) FILTER(WHERE last_at-first_at >= interval '30 days') AS change_span30_listings,
               count(*) FILTER(WHERE last_at-first_at >= interval '60 days') AS change_span60_listings
        FROM p
    """,
    "tables": """
        SELECT table_name FROM information_schema.tables
        WHERE table_schema='public' AND (table_name LIKE '%price%' OR table_name LIKE '%sale%'
          OR table_name LIKE '%transaction%' OR table_name LIKE '%snapshot%') ORDER BY table_name
    """,
}


async def audit(dsn: str) -> dict:
    import asyncpg

    conn = await asyncpg.connect(dsn, command_timeout=180,
        server_settings={"default_transaction_read_only": "on", "statement_timeout": "180000"})
    try:
        async with conn.transaction(readonly=True, isolation="repeatable_read"):
            read_only = await conn.fetchval("SHOW transaction_read_only")
            if read_only != "on":
                raise RuntimeError("Forecast audit requires a read-only database transaction")
            report = {"database_time": await conn.fetchval("SELECT now()"),
                      "transaction_read_only": read_only}
            for name, sql in QUERIES.items():
                report[name] = [dict(row) for row in await conn.fetch(sql)]
            return report
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    from dotenv import load_dotenv

    repo_env = Path(__file__).resolve().parents[1] / ".env"
    load_dotenv(repo_env if repo_env.exists() else Path.cwd() / ".env")
    report = asyncio.run(audit(os.environ["DATABASE_URL"]))
    result = json.dumps(report, ensure_ascii=False, default=str, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(result + "\n", encoding="utf-8")
    else:
        print(result)


if __name__ == "__main__":
    main()
