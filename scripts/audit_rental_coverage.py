#!/usr/bin/env python3
"""Aggregate-only rental coverage audit, with a enforced read-only transaction.

Never initialises the application pool, applies migrations, contacts Krisha,
modifies listings, or exports individual cards/contact/address/photo data.
The database does not prove market completeness: this report distinguishes
collection freshness, source-enumeration evidence and analytics eligibility.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path


QUERIES = {
    "inventory_by_type": """
        SELECT coalesce(prop_type,'unknown') AS prop_type,count(*) AS total,
          count(*) FILTER(WHERE is_active IS TRUE) AS active,
          count(*) FILTER(WHERE is_active IS FALSE) AS archived,
          count(*) FILTER(WHERE is_active IS TRUE AND NOT coalesce(is_duplicate,false)
            AND NOT coalesce(dup_needs_review,false) AND archived_at IS NULL
            AND nullif(btrim(duplicate_of),'') IS NULL) AS active_clean,
          count(*) FILTER(WHERE last_seen>=now()-interval '1 hour') AS seen_1h,
          count(*) FILTER(WHERE last_seen>=now()-interval '24 hours') AS seen_24h,
          count(*) FILTER(WHERE last_seen>=now()-interval '72 hours') AS seen_72h,
          count(*) FILTER(WHERE last_seen>=now()-interval '7 days') AS seen_7d,
          count(*) FILTER(WHERE found_at>=now()-interval '24 hours') AS new_24h,
          count(*) FILTER(WHERE found_at>=now()-interval '7 days') AS new_7d,
          max(found_at) AS last_new,max(last_seen) AS last_observed,
          count(*) FILTER(WHERE is_active AND last_seen<now()-interval '72 hours') AS active_not_seen_72h,
          count(*) FILTER(WHERE is_active IS FALSE AND last_seen>archived_at) AS archived_seen_after_archiving,
          count(*) FILTER(WHERE last_seen IS NULL) AS missing_last_seen,
          count(*) FILTER(WHERE coalesce(is_duplicate,false)) AS duplicates,
          count(*) FILTER(WHERE coalesce(dup_needs_review,false)) AS duplicate_needs_review
        FROM rental_listings GROUP BY 1 ORDER BY 1
    """,
    "field_coverage_active_clean": """
        SELECT coalesce(prop_type,'unknown') AS prop_type,count(*) AS rows,
          count(*) FILTER(WHERE last_seen>=now()-interval '72 hours') AS fresh_72h,
          count(*) FILTER(WHERE nullif(btrim(address),'') IS NOT NULL) AS has_address,
          count(*) FILTER(WHERE nullif(btrim(district),'') IS NOT NULL) AS has_district,
          count(*) FILTER(WHERE nullif(btrim(complex_name),'') IS NOT NULL) AS has_complex_name,
          count(*) FILTER(WHERE nullif(btrim(complex_url),'') IS NOT NULL) AS has_source_complex_link,
          count(*) FILTER(WHERE lat BETWEEN 50.8 AND 51.5 AND lon BETWEEN 71 AND 71.9) AS has_astana_geo,
          count(*) FILTER(WHERE nullif(btrim(photo_url),'') IS NOT NULL
            OR CASE WHEN jsonb_typeof(photos)='array' THEN jsonb_array_length(photos)>0 ELSE false END) AS has_photo,
          count(*) FILTER(WHERE CASE WHEN jsonb_typeof(photos)='array' THEN jsonb_array_length(photos)>=5 ELSE false END) AS has_5_photos,
          count(*) FILTER(WHERE area>0) AS has_area,
          count(*) FILTER(WHERE rooms IS NOT NULL) AS has_rooms,
          count(*) FILTER(WHERE floor IS NOT NULL AND floors_total IS NOT NULL) AS has_floor_pair,
          count(*) FILTER(WHERE nullif(btrim(published_at),'') IS NOT NULL) AS has_source_published_label,
          count(*) FILTER(WHERE title ~* 'посуточ|сутк|почас|тәулік|сағат|per (day|night|hour)') AS contrary_short_term_title
        FROM rental_listings WHERE is_active IS TRUE AND NOT coalesce(is_duplicate,false)
          AND NOT coalesce(dup_needs_review,false) AND archived_at IS NULL
          AND nullif(btrim(duplicate_of),'') IS NULL
        GROUP BY 1 ORDER BY 1
    """,
    "archive_reasons": """
        SELECT prop_type,coalesce(archive_reason,'unknown') AS reason,count(*) AS rows,
          count(*) FILTER(WHERE archived_at>=now()-interval '24 hours') AS archived_24h,
          count(*) FILTER(WHERE archived_at>=now()-interval '7 days') AS archived_7d,
          min(archived_at) AS first_at,max(archived_at) AS last_at
        FROM rental_listings WHERE is_active IS FALSE GROUP BY 1,2 ORDER BY 1,2
    """,
    "arrival_days": """
        SELECT (found_at AT TIME ZONE 'Asia/Almaty')::date AS day,prop_type,count(*) AS new_observations
        FROM rental_listings WHERE found_at>=now()-interval '14 days'
        GROUP BY 1,2 ORDER BY 1,2
    """,
    "archive_days": """
        SELECT (archived_at AT TIME ZONE 'Asia/Almaty')::date AS day,prop_type,
          coalesce(archive_reason,'unknown') AS reason,count(*) AS archived_observations
        FROM rental_listings WHERE archived_at>=now()-interval '14 days'
        GROUP BY 1,2,3 ORDER BY 1,2,3
    """,
    "legacy_index_inputs": """
        SELECT prop_type,count(*) AS rows,
          count(*) FILTER(WHERE is_active IS FALSE OR archived_at IS NOT NULL) AS archived_rows,
          count(*) FILTER(WHERE coalesce(is_duplicate,false) OR coalesce(dup_needs_review,false)
            OR nullif(btrim(duplicate_of),'') IS NOT NULL) AS duplicate_or_disputed_rows,
          count(*) FILTER(WHERE last_seen<now()-interval '72 hours' OR last_seen IS NULL) AS not_fresh_72h,
          count(*) FILTER(WHERE title ~* 'посуточ|сутк|почас|тәулік|сағат|per (day|night|hour)') AS contrary_short_term_title
        FROM rental_listings WHERE coalesce(last_seen,found_at)>now()-interval '30 days'
          AND price>15000 AND price<2000000 GROUP BY 1 ORDER BY 1
    """,
    "legacy_index_freshness": """
        SELECT prop_type,count(*) AS groups,
          count(*) FILTER(WHERE updated_at>=now()-interval '24 hours') AS updated_24h,
          count(*) FILTER(WHERE updated_at<now()-interval '72 hours') AS stale_72h,
          count(*) FILTER(WHERE sample_count<5) AS samples_below_5,
          min(updated_at) AS oldest_updated,max(updated_at) AS latest_updated,
          max(sample_count) AS largest_sample
        FROM rental_index GROUP BY 1 ORDER BY 1
    """,
    "sale_estimates": """
        SELECT count(*) AS active_clean_sales,
          count(*) FILTER(WHERE est_rent>0) AS with_stored_rent,
          count(*) FILTER(WHERE yield_pct>0) AS with_stored_yield,
          count(*) FILTER(WHERE est_rent>0 AND last_seen<now()-interval '72 hours') AS estimates_on_sales_not_seen_72h,
          count(*) FILTER(WHERE est_rent>0 AND rent_source LIKE 'ЖК%') AS estimate_level_complex,
          count(*) FILTER(WHERE est_rent>0 AND rent_source LIKE 'район%') AS estimate_level_district,
          count(*) FILTER(WHERE est_rent>0 AND rent_source LIKE 'город%') AS estimate_level_city
        FROM apartment_listings WHERE is_active IS TRUE AND NOT coalesce(is_duplicate,false)
    """,
    "price_history": """
        SELECT count(*) AS events,count(DISTINCT listing_id) AS listings,
          count(*) FILTER(WHERE changed_at>=now()-interval '24 hours') AS changes_24h,
          count(*) FILTER(WHERE changed_at>=now()-interval '7 days') AS changes_7d,
          max(changed_at) AS last_change FROM rental_price_history
    """,
    "rental_schema": """
        SELECT column_name,data_type FROM information_schema.columns
        WHERE table_schema='public' AND table_name='rental_listings'
        ORDER BY ordinal_position
    """,
    "legacy_three_day_disappearance": """
        SELECT prop_type,count(*) AS falsely_inferred_gone
        FROM rental_listings WHERE is_active IS TRUE AND last_seen<now()-interval '3 days'
        GROUP BY 1 ORDER BY 1
    """,
    "apartment_map_scope": """
        SELECT count(*) AS rows_before_1000_cap,
          count(*) FILTER(WHERE is_active IS TRUE) AS active,
          count(*) FILTER(WHERE is_active IS FALSE) AS archived,
          count(*) FILTER(WHERE lat IS NULL OR lon IS NULL) AS no_own_geo,
          count(*) FILTER(WHERE coalesce(dup_needs_review,false)) AS duplicate_needs_review,
          count(*) FILTER(WHERE last_seen>=now()-interval '72 hours' AND is_active IS TRUE) AS active_fresh_72h
        FROM rental_listings WHERE prop_type='apartment'
          AND ((is_active IS NOT FALSE AND last_seen>now()-interval '30 days')
            OR (is_active IS FALSE AND archived_at>now()-interval '30 days'))
          AND NOT coalesce(is_duplicate,false)
    """,
}

SETTING_SQL = """
    SELECT key,value FROM app_settings
    WHERE key LIKE 'RENTAL_CRAWL_V1_%'
      OR key LIKE 'RENTAL_METRICS_V1_%'
      OR key IN ('RENTAL_SERVICE_CYCLE_LAST','RENTAL_SERVICE_REVISION','RENTAL_SERVICE_STARTED_AT')
      OR key IN ('RENTAL_BACKFILL_BATCH','RENTAL_ARCHIVE_CHECK_BATCH','SHEETS_RENTAL_SYNCED_AT')
    ORDER BY key
"""


def setting_projection(rows: list[dict]) -> dict:
    """Cursor signatures/settings may expand; expose only approved health fields."""
    result = {}
    suffixes = ('_STARTED_AT','_COMPLETED_AT','_DURATION_SEC','_LAST_PAGES','_EXPECTED_TOTAL',
                '_EXPECTED_LAST_PAGE','_UNIQUE_SEEN','_TOTAL','_ABORTED_AT','_ABORT_REASON',
                '_LAST_STATUS','_LAST_SUCCESS_AT','_COMPLETED_VERIFIED','_INCOMPLETE_COMPLETED_AT','_INCOMPLETE_DURATION_SEC',
                '_INCOMPLETE_LAST_PAGES','_INCOMPLETE_UNIQUE_SEEN','_INCOMPLETE_TOTAL')
    for row in rows:
        key,value = row['key'],row['value']
        if key in ('RENTAL_BACKFILL_BATCH','RENTAL_ARCHIVE_CHECK_BATCH','SHEETS_RENTAL_SYNCED_AT',
                   'RENTAL_SERVICE_REVISION','RENTAL_SERVICE_STARTED_AT'):
            result[key] = value
        elif key in ('RENTAL_CRAWL_V1_APARTMENT','RENTAL_CRAWL_V1_PARKING','RENTAL_CRAWL_V1_COMMERCIAL'):
            try:
                cursor = json.loads(value)
                page = cursor.get('page')
                signatures = cursor.get('signatures')
                result[key] = {'page': page if type(page) is int and page>=1 else None,
                               'seen_page_signatures': len(signatures) if isinstance(signatures,list) else None}
                for field in ('pages_seen','expected_last_page','expected_total'):
                    number = cursor.get(field)
                    if type(number) is int and number>=0:
                        result[key][field] = number
                if isinstance(cursor.get('seen_ids'),list):
                    result[key]['unique_seen_ids'] = len(set(cursor['seen_ids']))
            except (ValueError,TypeError,AttributeError):
                result[key] = {'invalid_cursor': True}
        elif any(key==f'RENTAL_CRAWL_V1_{kind}{suffix}' for kind in ('APARTMENT','PARKING','COMMERCIAL') for suffix in suffixes):
            result[key] = value
        elif key=='RENTAL_SERVICE_CYCLE_LAST':
            try:
                cycle = json.loads(value)
                if isinstance(cycle,dict):
                    result[key] = {name:cycle.get(name) for name in ('at','type','cycle','list_status','page','saved',
                        'head_pages','durable_pages','list_seconds','maintenance_seconds','work_seconds','sleep_seconds')}
            except (ValueError,TypeError):
                result[key] = {'invalid_metrics':True}
        elif key.startswith('RENTAL_METRICS_V1_'):
            from bot.core.rental_service_metrics import COUNTERS
            try:
                counters = json.loads(value)
                if isinstance(counters,dict):
                    result[key] = {name:int(counters[name]) for name in COUNTERS
                        if name in counters and type(counters[name]) is int and counters[name]>=0}
            except (ValueError,TypeError):
                result[key] = {'invalid_metrics':True}
    return result


def artifact_projection(root: Path) -> dict:
    """Read only count/timing metadata; never publish the underlying cards."""
    try:
        content = json.loads((root/'var/market-comparison.json').read_text(encoding='utf-8'))
    except (OSError,ValueError,TypeError):
        return {'available': False}
    if not isinstance(content,dict):
        return {'available': False}
    cells = content.get('cells',[])
    offers = []
    if isinstance(cells,list):
        for cell in cells:
            if not isinstance(cell,dict):
                continue
            for side in ('primary','secondary'):
                side_data = cell.get(side,{})
                side_offers = side_data.get('offers',[]) if isinstance(side_data,dict) else []
                if isinstance(side_offers,list):
                    offers.extend(offer for offer in side_offers if isinstance(offer,dict))
    # Current snapshot stores a compact offers array at the top level.
    if isinstance(content.get('offers'),list):
        offers = [row for row in content['offers'] if isinstance(row,dict)]
    return {'available': True,'schema_version': content.get('schema_version'),
            'generated_at': content.get('generated_at'),'offers': len(offers),
            'with_rental_estimate': sum(isinstance(row.get('rental_estimate'),dict) for row in offers)}


async def audit(dsn: str, root: Path) -> dict:
    import asyncpg
    from bot.core.comparison_rental_estimate import RENTAL_COMPARISON_SQL

    connection = await asyncpg.connect(dsn,command_timeout=60,server_settings={
        'default_transaction_read_only':'on','statement_timeout':'60000',
        'application_name':'hatuli_rental_coverage_readonly'})
    try:
        async with connection.transaction(readonly=True,isolation='repeatable_read'):
            read_only = await connection.fetchval('SHOW transaction_read_only')
            if read_only!='on':
                raise RuntimeError('Rental audit requires a read-only transaction')
            now = await connection.fetchval('SELECT now()')
            report = {'database_time': now,'transaction_read_only':read_only,'database_writes':0,
                      'scope':'Astana Krisha monitored apartment/parking/commercial categories; not whole rental market',
                      'market_completeness':'unknown_without_independent_source_enumeration'}
            for name,query in QUERIES.items():
                report[name] = [dict(row) for row in await connection.fetch(query)]
            report['crawler_settings'] = setting_projection([dict(row) for row in await connection.fetch(SETTING_SQL)])
            # This aggregate uses exactly the offline comparison publisher's SQL,
            # including canonical name ambiguity and source freshness predicates.
            clean_query = RENTAL_COMPARISON_SQL.rsplit('ORDER BY',1)[0]
            groups = await connection.fetch('WITH qualified AS ('+clean_query+''')
                SELECT complex_id,rooms,count(*) AS rows
                FROM qualified GROUP BY complex_id,rooms ORDER BY rows DESC''',now)
            report['strict_comparison_pool'] = {
                'qualified_rows':sum(row['rows'] for row in groups),'complex_room_groups':len(groups),
                'largest_group':max((row['rows'] for row in groups),default=0),
                'groups_with_at_least_5_before_area_dedup_checks':sum(row['rows']>=5 for row in groups),
                'max_age_hours':72,'minimum_final_unique_comparables':5,'area_band_percent':15,
                'period_is_source_monthly_category_provenance':True,
                'period_database_field_present':any(row['column_name'] in ('rent_period','rental_period') for row in report['rental_schema'])}
            report['public_comparison_snapshot'] = artifact_projection(root)
            return report
    finally:
        await connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path.cwd())
    parser.add_argument('--output',type=Path)
    args = parser.parse_args()
    from dotenv import load_dotenv
    load_dotenv(args.root/'.env')
    report = asyncio.run(audit(os.environ['DATABASE_URL'],args.root))
    text = json.dumps(report,ensure_ascii=False,default=str,indent=2)+'\n'
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(text,encoding='utf-8')
    else:
        print(text,end='')


if __name__=='__main__':
    main()
