#!/usr/bin/env python3
"""Aggregate-only audit of monthly-rental forecasting evidence; no model training.

Use raw asyncpg with read-only connection AND repeatable-read transaction. No
application pool, migrations, source requests, IDs, addresses or contacts are
exported. Legacy price changes do not prove historic monthly/daily price units.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path


QUERIES = {
    "inventory": """
        SELECT rent_period, count(*) AS listings,
               count(*) FILTER(WHERE is_active AND archived_at IS NULL) AS active,
               count(*) FILTER(WHERE is_active AND archived_at IS NULL
                   AND last_seen BETWEEN now()-interval '72 hours' AND now()) AS fresh_active,
               count(*) FILTER(WHERE rent_period_conflict) AS period_conflicts,
               count(*) FILTER(WHERE rental_episode_started_at IS NOT NULL) AS source_proved_episodes
        FROM rental_listings WHERE city='astana' AND prop_type='apartment'
        GROUP BY rent_period ORDER BY rent_period
    """,
    "legacy_price_changes": """
        SELECT count(*) AS events,count(DISTINCT h.listing_id) AS listings,
               min(h.changed_at) AS first_at,max(h.changed_at) AS last_at,
               count(*) FILTER(WHERE new_price<old_price) AS cuts,
               count(*) FILTER(WHERE new_price>old_price) AS raises,
               count(*) FILTER(WHERE r.rent_period='unknown') AS current_period_unknown,
               count(*) FILTER(WHERE new_price<=0 OR old_price<=0 OR new_price IS NULL OR old_price IS NULL) AS invalid
        FROM rental_price_history h JOIN rental_listings r ON r.id=h.listing_id
        WHERE r.city='astana' AND r.prop_type='apartment'
    """,
    "source_observations": """
        SELECT rent_period,count(*) AS observations,count(DISTINCT listing_id) AS listings,
               count(DISTINCT observed_on) AS observed_days,
               min(observed_at) AS first_at,max(observed_at) AS last_at,
               count(*) FILTER(WHERE period_source_checked) AS period_proved,
               count(*) FILTER(WHERE area>0 AND rooms BETWEEN 1 AND 6) AS observed_attributes,
               count(*) FILTER(WHERE complex_id IS NOT NULL) AS has_complex_id,
               count(*) FILTER(WHERE hex_id IS NOT NULL AND hex_edge_m IS NOT NULL) AS observed_location
        FROM rental_observations WHERE city='astana'
        GROUP BY rent_period ORDER BY rent_period
    """,
    "monthly_history_spans": """
        WITH spans AS (
          SELECT listing_id,episode_started_at,count(*) AS n,
                 max(observed_at)-min(observed_at) AS span,
                 count(DISTINCT price) AS prices
          FROM rental_observations WHERE city='astana' AND rent_period='monthly'
            AND period_source_checked
          GROUP BY listing_id,episode_started_at
        )
        SELECT count(*) AS listing_episodes,
               count(*) FILTER(WHERE n>=2) AS repeat_observed_episodes,
               count(*) FILTER(WHERE prices>=2) AS changed_asking_price_episodes,
               count(*) FILTER(WHERE span>=interval '7 days') AS span7_episodes,
               count(*) FILTER(WHERE span>=interval '14 days') AS span14_episodes,
               count(*) FILTER(WHERE span>=interval '30 days') AS span30_episodes
        FROM spans
    """,
    "monthly_future_price_pairs": """
        WITH eligible AS (
          SELECT o.* FROM rental_observations o JOIN rental_listings r ON r.id=o.listing_id
          WHERE o.city='astana' AND o.rent_period='monthly' AND o.period_source_checked
            AND r.prop_type='apartment' AND NOT coalesce(r.is_duplicate,FALSE)
            AND NOT coalesce(r.dup_needs_review,FALSE) AND nullif(btrim(r.duplicate_of),'') IS NULL
            AND NOT coalesce(r.rent_period_conflict,FALSE)
            AND o.price BETWEEN 50000 AND 2000000 AND o.area>0
            AND o.observed_at<=now()
        ), pairs AS (
          SELECT h.days,o.listing_id,o.price AS current_price,t.price AS future_price
          FROM (VALUES(7),(14),(30)) h(days)
          CROSS JOIN eligible o
          LEFT JOIN eligible t ON t.listing_id=o.listing_id
            AND t.episode_started_at=o.episode_started_at AND t.observed_on=o.observed_on+h.days
            AND t.rooms=o.rooms AND t.area=o.area
            AND t.district IS NOT DISTINCT FROM o.district
            AND t.complex_id IS NOT DISTINCT FROM o.complex_id
            AND t.hex_id IS NOT DISTINCT FROM o.hex_id
            AND t.hex_edge_m IS NOT DISTINCT FROM o.hex_edge_m
          WHERE o.observed_on+h.days<=(now() AT TIME ZONE 'UTC')::date
        )
        SELECT h.days AS horizon_days,count(p.listing_id) AS matured_anchor_points,
               count(p.future_price) AS source_proved_future_pairs,
               count(DISTINCT p.listing_id) FILTER(WHERE p.future_price IS NOT NULL) AS listings_with_pairs,
               count(DISTINCT p.listing_id) FILTER(WHERE p.future_price<>p.current_price) AS changed_price_listings,
               count(p.listing_id) FILTER(WHERE p.future_price IS NULL) AS missing_future_outcome_not_unchanged
        FROM (VALUES(7),(14),(30)) h(days) LEFT JOIN pairs p ON p.days=h.days
        GROUP BY h.days ORDER BY h.days
    """,
    "archive_evidence": """
        SELECT rent_period,archive_reason,count(*) AS listings,
               min(archived_at) AS first_at,max(archived_at) AS last_at
        FROM rental_listings WHERE city='astana' AND prop_type='apartment' AND archived_at IS NOT NULL
        GROUP BY rent_period,archive_reason ORDER BY rent_period,archive_reason
    """,
    "monthly_removal_cohort": """
        WITH proof AS (
          SELECT o.listing_id,o.episode_started_at,min(o.observed_at) AS first_proof,
                 max(o.observed_at) AS last_proof,count(DISTINCT o.observed_on) AS proof_days
          FROM rental_observations o WHERE o.city='astana' AND o.rent_period='monthly'
            AND o.period_source_checked AND o.observed_at<=now()
          GROUP BY o.listing_id,o.episode_started_at
        ), episodes AS (
          SELECT r.*,p.first_proof,p.last_proof,p.proof_days,
                 (NOT r.is_active AND r.archive_reason IN ('archived_badge','confirmed_gone')
                  AND r.archived_at BETWEEN p.first_proof+interval '2 days' AND now()
                  AND p.proof_days>=2
                  AND p.last_proof BETWEEN r.archived_at-interval '72 hours' AND r.archived_at
                  AND r.rental_episode_last_seen_at<=r.archived_at) AS confirmed_removal
          FROM proof p JOIN rental_listings r ON r.id=p.listing_id
            AND r.rental_episode_started_at=p.episode_started_at
          WHERE r.city='astana' AND r.prop_type='apartment' AND r.rent_period='monthly'
            AND NOT coalesce(r.is_duplicate,FALSE) AND NOT coalesce(r.dup_needs_review,FALSE)
            AND nullif(btrim(r.duplicate_of),'') IS NULL AND NOT coalesce(r.rent_period_conflict,FALSE)
        )
        SELECT count(*) AS current_proved_episodes,
               count(*) FILTER(WHERE proof_days>=2) AS repeat_source_proved_episodes,
               count(*) FILTER(WHERE confirmed_removal) AS source_confirmed_removals,
               count(*) FILTER(WHERE is_active AND archived_at IS NULL) AS active_right_censored,
               count(*) FILTER(WHERE NOT is_active AND NOT coalesce(confirmed_removal,FALSE)) AS unknown_or_unqualified_exit,
               min(first_proof) AS first_proof_at,max(last_proof) AS last_proof_at
        FROM episodes
    """,
    "rental_outcome_schema": """
        SELECT table_name,column_name,data_type FROM information_schema.columns
        WHERE table_schema='public'
          AND (table_name LIKE '%rental%' OR table_name LIKE '%lease%' OR table_name='property_listings')
          AND (column_name LIKE '%contract%' OR column_name LIKE '%achieved%'
            OR column_name LIKE '%lease%' OR column_name LIKE '%let_%'
            OR column_name LIKE '%property_id%' OR column_name LIKE '%listing_id%')
        ORDER BY table_name,ordinal_position
    """,
}


def artifact_audit(root: Path) -> dict:
    """Only aggregate model metadata; never copy per-offer results or features."""
    path = root / "var/rental-forecasts.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) if path.stat().st_size<=20_000_000 else {}
    except (OSError, ValueError, TypeError):
        raw = {}
    if not isinstance(raw,dict):
        raw = {}
    reports = raw.get('reports',[])
    return {'present': bool(raw), 'schema_version': raw.get('schema_version'),
            'method_version': raw.get('method_version'), 'generated_at': raw.get('generated_at'),
            'source_history_verified': raw.get('source_history_verified') is True,
            'independent_property_groups_verified': raw.get('independent_property_groups_verified') is True,
            'published_offers':len(raw.get('offers',{})) if isinstance(raw.get('offers'),dict) else 0,
            'reports':[{'target':r.get('target'),'horizon_days':r.get('horizon_days'),
                        'status':r.get('status'),'fold_count':r.get('fold_count')}
                       for r in reports if isinstance(r,dict)] if isinstance(reports,list) else []}


def conclusions(report: dict) -> dict:
    monthly=next((row for row in report['source_observations'] if row['rent_period']=='monthly'),{})
    spans=next(iter(report['monthly_history_spans']),{})
    removals=next(iter(report['monthly_removal_cohort']),{})
    return {
        'asking_price_target':'future_asking_rent_conditional_on_remaining_listed',
        'removal_target':'remaining_days_to_source_confirmed_removal_not_letting',
        'legacy_price_history_is_period_verified':False,
        'source_observed_monthly_days':monthly.get('observed_days',0),
        'monthly_episodes_spanning_7_days':spans.get('span7_episodes',0),
        'source_confirmed_monthly_removals':removals.get('source_confirmed_removals',0),
        'minimum_independent_test_properties':200,'minimum_changed_price_test_properties':50,
        'minimum_temporal_folds':2,
        'artifact_present':report['artifact']['present'],
        'model_validation_recomputed_by_this_audit':False,
        'reason_codes':[
            code for condition,code in (
                (not report['artifact']['present'],'missing_validated_rental_artifact'),
                (monthly.get('observed_days',0)<7,'insufficient_source_verified_monthly_history'),
                (not spans.get('span7_episodes',0),'no_matured_7_day_asking_price_outcomes'),
                (removals.get('source_confirmed_removals',0)<200,'insufficient_confirmed_removal_outcomes'),
                (True,'independent_rental_property_identity_and_temporal_backtest_required'),
            ) if condition],
    }


async def audit(dsn: str, root: Path) -> dict:
    import asyncpg
    conn=await asyncpg.connect(dsn,command_timeout=60,server_settings={
        'default_transaction_read_only':'on','statement_timeout':'60000',
        'application_name':'hatuli_rental_forecast_readiness_readonly'})
    try:
        async with conn.transaction(readonly=True,isolation='repeatable_read'):
            readonly=await conn.fetchval('SHOW transaction_read_only')
            if readonly!='on':
                raise RuntimeError('Rental forecasting audit requires a read-only transaction')
            report={'database_time':await conn.fetchval('SELECT now()'),
                    'transaction_read_only':readonly,'database_writes':0,'source_requests':0}
            for name,sql in QUERIES.items():
                report[name]=[dict(row) for row in await conn.fetch(sql)]
            report['artifact']=artifact_audit(root)
            report['readiness']=conclusions(report)
            return report
    finally:
        await conn.close()


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path.cwd())
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    from dotenv import load_dotenv
    load_dotenv(args.root/'.env')
    report=asyncio.run(audit(os.environ['DATABASE_URL'],args.root))
    text=json.dumps(report,ensure_ascii=False,default=str,indent=2)+'\n'
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(text,encoding='utf-8')
        try:
            args.output.chmod(0o600)
        except OSError:
            pass
    else:
        print(text,end='')


if __name__=='__main__':
    main()
