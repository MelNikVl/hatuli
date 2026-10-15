"""Apply a source-reviewed identity plan atomically with a before/after manifest.

Install schema migrations first. Validate the complete plan on a restored copy
before using --apply on a running database. The default only prints the plan.
No complex, listing, property, source-link or historical snapshot is deleted.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit, urlunsplit

import asyncpg
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot.core.complex_binding import build_preview_sql, build_sql
from bot.core.complex_canonical import _apply_canonical
from bot.core.complex_ingest_identity import record_complex_observations
from bot.core.complex_metrics import CURRENT_METRICS_SQL
from complex_stats_snapshot import SNAPSHOT_SQL


def encoded(value):
    return json.dumps(value, ensure_ascii=False, default=str)


async def apply_plan(conn, plan: dict, output: Path) -> dict:
    async with conn.transaction(isolation='repeatable_read'):
        await conn.fetchval('SELECT pg_advisory_xact_lock(728143)')
        before = [dict(r) for r in await conn.fetch('SELECT * FROM complexes ORDER BY id FOR UPDATE')]
        known = {r['id']: r for r in before}
        before_overrides = [dict(r) for r in await conn.fetch('SELECT * FROM complex_canonical_overrides')]
        if before_overrides:
            raise ValueError('This initial rollout requires an empty override registry; review a new plan for later changes')
        before_relations = [dict(r) for r in await conn.fetch('SELECT * FROM complex_relations')]
        before_aliases = [dict(r) for r in await conn.fetch('SELECT * FROM complex_aliases')]
        before_addresses = [dict(r) for r in await conn.fetch('SELECT * FROM complex_addresses')]
        before_rejections = [dict(r) for r in await conn.fetch('SELECT * FROM complex_source_rejections')]
        if before_relations or before_aliases or before_addresses or before_rejections:
            raise ValueError('Initial rollout requires empty review/observation registries; rebuild the plan for existing decisions')
        stats_date = await conn.fetchval('SELECT CURRENT_DATE')
        inventory_before = dict(await conn.fetchrow('SELECT count(*) AS listings FROM apartment_listings'))
        before_today = [dict(r) for r in await conn.fetch('SELECT * FROM complex_stats_history WHERE date=CURRENT_DATE')]
        for change in plan.get('field_corrections', []):
            cid = change['complex_id']
            if change['field'] != 'krisha_url' or cid not in known:
                raise ValueError('Only reviewed source URL correction is supported')
            if known[cid]['krisha_url'] != change['before']:
                raise ValueError(f'Source URL changed since review: {cid}')
            await conn.execute('UPDATE complexes SET krisha_url=$2 WHERE id=$1', cid, change['after'])
            await conn.execute('''INSERT INTO complex_source_rejections
                (complex_id,source,source_id,evidence,reviewed_by) VALUES($1,'krisha',$2,$3::jsonb,$4)''',
                cid,change['before'],encoded(change['evidence']),plan['reviewed_by'])
        for relation in plan.get('relations_for_import', []):
            a, b = relation['complex_id_a'], relation['complex_id_b']
            if a >= b or a not in known or b not in known:
                raise ValueError('Invalid relation IDs/order')
            await conn.execute('''INSERT INTO complex_relations
                (complex_id_a,complex_id_b,relation_type,confidence,evidence,reviewed_by,reviewed_at,methodology_version)
                VALUES($1,$2,$3,$4,$5::jsonb,$6,now(),$7)
                ON CONFLICT(complex_id_a,complex_id_b) DO NOTHING''',
                a,b,relation['relation_type'],relation.get('confidence',1),encoded(relation['evidence']),
                relation.get('reviewed_by',plan['reviewed_by']),relation.get('methodology_version','complex_source_audit_v1'))
        for decision in plan.get('canonical_overrides', []):
            cid, target = decision['complex_id'], decision['canonical_id']
            if cid not in known or (target is not None and target not in known):
                raise ValueError('Unknown manual mapping ID')
            evidence = encoded(decision['evidence'])
            await conn.execute('''INSERT INTO complex_canonical_overrides
                (complex_id,canonical_id,evidence,reviewed_by) VALUES($1,$2,$3::jsonb,$4)''',
                cid,target,evidence,plan['reviewed_by'])
            await conn.execute('''INSERT INTO complex_canonical_override_log
                (complex_id,previous_canonical_id,canonical_id,evidence,reviewed_by)
                VALUES($1,$2,$3,$4::jsonb,$5)''',cid,known[cid]['canonical_id'],target,evidence,plan['reviewed_by'])
        # Seed observations, preserving every original record/source ID. They
        # do not participate in resolution until explicitly reviewed below.
        for row in before:
            if row['is_garbage'] or row['is_street']:
                continue
            await record_complex_observations(row['id'],'catalog_snapshot',str(row['id']),
                row['name'],row['address'],evidence={'catalog_id':row['id']},connection=conn)
        for item in plan.get('verified_aliases', []):
            await conn.execute('''INSERT INTO complex_aliases
                (complex_id,name,normalized_name,source,source_id,evidence,status,reviewed_by,reviewed_at)
                VALUES($1,$2,complex_name_key($2),$5,$6,$3::jsonb,'verified',$4,now())
                ON CONFLICT(complex_id,source,source_id,name) DO UPDATE SET status='verified',
                    evidence=EXCLUDED.evidence,reviewed_by=EXCLUDED.reviewed_by,reviewed_at=now()''',
                item['complex_id'],item['name'],encoded(item['evidence']),plan['reviewed_by'],
                item.get('source','source_review'),item.get('source_id',str(item['complex_id'])))
        for item in plan.get('verified_addresses', []):
            await conn.execute('''INSERT INTO complex_addresses
                (complex_id,address,normalized_address,source,source_id,evidence,status,reviewed_by,reviewed_at,
                 city,street_key,house_number)
                VALUES($1,$2,lower(btrim($2)),$5,$6,$3::jsonb,'verified',$4,now(),$7,$8,$9)
                ON CONFLICT(complex_id,source,source_id,address) DO UPDATE SET status='verified',
                    evidence=EXCLUDED.evidence,reviewed_by=EXCLUDED.reviewed_by,reviewed_at=now()''',
                item['complex_id'],item['address'],encoded(item['evidence']),plan['reviewed_by'],
                item.get('source','source_review'),item.get('source_id',str(item['complex_id'])),
                item.get('city'),item.get('street_key'),item.get('house_number'))
        canonical_stats = await _apply_canonical(conn)
        after = [dict(r) for r in await conn.fetch('SELECT * FROM complexes ORDER BY id')]
        changes = [dict(r) for r in await conn.fetch(build_preview_sql())]
        listing_ids = [r['listing_id'] for r in changes]
        properties = [dict(r) for r in await conn.fetch('''SELECT DISTINCT p.property_id,p.complex_id
            FROM properties p LEFT JOIN property_listings pl ON pl.property_id=p.property_id
            WHERE pl.listing_id=ANY($1::text[]) OR p.complex_id=ANY($2::int[]) ORDER BY p.property_id''',
            listing_ids, [r['id'] for r in after if (r['canonical_id'],r['canonical_reason']) !=
                          (known[r['id']]['canonical_id'],known[r['id']]['canonical_reason'])])]
        binding_stats = dict(await conn.fetchrow(build_sql(False)))
        await conn.execute(CURRENT_METRICS_SQL)
        # Today's complete snapshot is recomputed, including groups that lost
        # their last active listing. Prior dates remain untouched.
        await conn.execute('DELETE FROM complex_stats_history WHERE date=$1',stats_date)
        await conn.execute(SNAPSHOT_SQL)
        inventory_after = dict(await conn.fetchrow('SELECT count(*) AS listings FROM apartment_listings'))
        if inventory_after != inventory_before or binding_stats['changed'] != len(changes):
            raise ValueError('Inventory or proposed/actual transition count changed')
        remainder = await conn.fetch(build_preview_sql())
        if remainder:
            raise ValueError('Binding rollout is not idempotent')
        report = {'generated_at':datetime.now(timezone.utc).isoformat(), 'applied':False,
            'database':await conn.fetchval('SELECT current_database()'), 'plan':plan,
            'stats_date':str(stats_date),
            'inventory_before':inventory_before,'inventory_after':inventory_after,
            'canonical_stats':canonical_stats,'binding_stats':binding_stats,
            'before_complexes':before,'after_canonical':after,'before_overrides':before_overrides,
            'before_relations':before_relations,'before_aliases':before_aliases,'before_addresses':before_addresses,
            'before_rejections':before_rejections,
            'after_relations':[dict(r) for r in await conn.fetch('SELECT * FROM complex_relations')],
            'after_aliases':[dict(r) for r in await conn.fetch('SELECT * FROM complex_aliases')],
            'after_addresses':[dict(r) for r in await conn.fetch('SELECT * FROM complex_addresses')],
            'after_overrides':[dict(r) for r in await conn.fetch('SELECT * FROM complex_canonical_overrides')],
            'after_rejections':[dict(r) for r in await conn.fetch('SELECT * FROM complex_source_rejections')],
            'after_metrics':[dict(r) for r in await conn.fetch('''SELECT id,listings_count,sold_count,
                avg_price_m2,avg_yield,rental_listings_count FROM complexes ORDER BY id''')],
            'after_today_stats':[dict(r) for r in await conn.fetch('SELECT * FROM complex_stats_history WHERE date=$1',stats_date)],
            'before_today_stats':before_today,'listing_changes':changes,
            'affected_properties_unchanged':properties,
            'notes':['Source spine and property assignments retained. Historical snapshots retained.']}
        output.parent.mkdir(parents=True,exist_ok=True)
        output.write_text(encoded(report)+'\n',encoding='utf-8')
    report['applied'] = True
    output.write_text(encoded(report)+'\n',encoding='utf-8')
    return report


async def rollback_plan(conn, report: dict) -> dict:
    """Guarded inverse; preserve forward provenance and append reverse events."""
    after = {r['id']:r for r in report['after_canonical']}
    async with conn.transaction():
        if await conn.fetchval('SELECT current_database()') != report['database']:
            raise ValueError('Rollback manifest belongs to a different database')
        await conn.fetchval('SELECT pg_advisory_xact_lock(728143)')
        from datetime import date
        stats_date = date.fromisoformat(report['stats_date'])
        for table, label in [('complex_aliases','aliases'),('complex_addresses','addresses'),
                             ('complex_relations','relations'),('complex_canonical_overrides','overrides'),
                             ('complex_source_rejections','rejections')]:
            current = [dict(r) for r in await conn.fetch(f'SELECT * FROM {table} ORDER BY 1 FOR UPDATE')]
            expected = report['after_'+label]
            sort_key = lambda row: encoded(sorted(row.items()))
            if sorted(json.loads(encoded(current)),key=sort_key) != sorted(expected,key=sort_key):
                raise ValueError(f'Rollback registry changed since rollout: {table}')
        current_stats = [dict(r) for r in await conn.fetch('SELECT * FROM complex_stats_history WHERE date=$1 ORDER BY complex_id FOR UPDATE',stats_date)]
        if json.loads(encoded(current_stats)) != sorted(report['after_today_stats'],key=lambda r:r['complex_id']):
            raise ValueError('Rollback snapshot changed since rollout')
        for old in report['before_complexes']:
            expected = after[old['id']]
            current = await conn.fetchrow('SELECT canonical_id,canonical_reason,krisha_url FROM complexes WHERE id=$1 FOR UPDATE',old['id'])
            if current is None or any(current[k] != expected[k] for k in ['canonical_id','canonical_reason','krisha_url']):
                raise ValueError(f'Rollback baseline no longer matches complex {old["id"]}')
        current_metrics = [dict(r) for r in await conn.fetch('''SELECT id,listings_count,sold_count,
            avg_price_m2,avg_yield,rental_listings_count FROM complexes ORDER BY id''')]
        if json.loads(encoded(current_metrics)) != report['after_metrics']:
            raise ValueError('Rollback current metrics changed since rollout')
        for change in report['listing_changes']:
            current = await conn.fetchrow('SELECT complex_id,complex_resolution FROM apartment_listings WHERE id=$1 FOR UPDATE',change['listing_id'])
            if current is None or current['complex_id'] != change['complex_id'] or current['complex_resolution'] != change['method']:
                raise ValueError(f'Rollback baseline no longer matches listing {change["listing_id"]}')
        for change in report['listing_changes']:
            await conn.execute('''INSERT INTO listing_complex_resolution_log
                (listing_id,previous_complex_id,complex_id,previous_resolution_method,resolution_method,
                 confidence_tier,resolved_at,evidence,resolver_version)
                VALUES($1,$2,$3,$4,'manual_rollback','A',now(),$5::jsonb,'complex_identity_rollback_v1')''',
                change['listing_id'],change['complex_id'],change['previous_complex_id'],change['method'],
                encoded({'forward_manifest_generated_at':report['generated_at']}))
            await conn.execute('UPDATE apartment_listings SET complex_id=$2,complex_resolution=$3 WHERE id=$1',
                change['listing_id'],change['previous_complex_id'],change['previous_complex_resolution'])
        for old in report['before_complexes']:
            await conn.execute('''UPDATE complexes SET canonical_id=$2,canonical_reason=$3,krisha_url=$4,
                listings_count=$5,sold_count=$6,avg_price_m2=$7,avg_yield=$8,rental_listings_count=$9 WHERE id=$1''',
                old['id'],old['canonical_id'],old['canonical_reason'],old['krisha_url'],old['listings_count'],
                old['sold_count'],old['avg_price_m2'],old['avg_yield'],old['rental_listings_count'])
        for decision in report['plan']['canonical_overrides']:
            await conn.execute('''INSERT INTO complex_canonical_override_log
                (complex_id,previous_canonical_id,canonical_id,evidence,reviewed_by)
                VALUES($1,$2,$3,$4::jsonb,'manual_rollback')''',decision['complex_id'],decision['canonical_id'],
                next(r['canonical_id'] for r in report['before_complexes'] if r['id']==decision['complex_id']),
                encoded({'forward_manifest_generated_at':report['generated_at']}))
            await conn.execute('DELETE FROM complex_canonical_overrides WHERE complex_id=$1',decision['complex_id'])
        for item in report.get('after_rejections',[]):
            await conn.execute('DELETE FROM complex_source_rejections WHERE complex_id=$1 AND source=$2 AND source_id=$3',
                item['complex_id'],item['source'],item['source_id'])
        for table, key, label in [('complex_aliases','alias_id','aliases'),('complex_addresses','address_id','addresses'),
                                  ('complex_relations','relation_id','relations')]:
            previous = {r[key] for r in report['before_'+label]}
            created = [r[key] for r in report['after_'+label] if r[key] not in previous]
            await conn.execute(f'DELETE FROM {table} WHERE {key}=ANY($1::int[])',created)
        await conn.execute('DELETE FROM complex_stats_history WHERE date=$1',stats_date)
        await conn.execute('''INSERT INTO complex_stats_history SELECT * FROM
            jsonb_populate_recordset(NULL::complex_stats_history,$1::jsonb)''',encoded(report['before_today_stats']))
    return {'rolled_back':True,'listings_restored':len(report['listing_changes']),
            'forward_and_reverse_journal_preserved':True}


async def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan',type=Path)
    parser.add_argument('--rollback-manifest',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--env-file',type=Path)
    parser.add_argument('--database',help='Optional restored-copy database override')
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--migrate',action='store_true',help='Apply repository schema migrations before the rollout')
    args=parser.parse_args()
    if bool(args.plan) == bool(args.rollback_manifest):
        parser.error('provide exactly one of --plan or --rollback-manifest')
    plan=json.loads((args.plan or args.rollback_manifest).read_text(encoding='utf-8'))
    if not args.apply:
        print(encoded(plan)); return
    load_dotenv(args.env_file)
    dsn=os.environ['DATABASE_URL']
    if args.database:
        dsn=urlunsplit(urlsplit(dsn)._replace(path='/'+args.database))
    if args.migrate:
        from bot.db.pg import init_pool, close_pool
        await init_pool(dsn)
        await close_pool()
    conn=await asyncpg.connect(dsn,command_timeout=600)
    try:
        if args.rollback_manifest:
            report=await rollback_plan(conn,plan)
            args.output.write_text(encoded(report)+'\n',encoding='utf-8')
            print(encoded(report))
        else:
            report=await apply_plan(conn,plan,args.output)
            print(encoded({k:report[k] for k in ['database','applied','inventory_after','canonical_stats','binding_stats']}))
    finally:
        await conn.close()


if __name__=='__main__':
    asyncio.run(main())
