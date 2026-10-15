#!/usr/bin/env python3
"""Read-only monthly rental candidates; publish only browser-checked editions."""
from __future__ import annotations
import argparse
import asyncio
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from bot.core.rental_picks import build_rental_picks,publish_snapshot,snapshot_path


async def read_candidates(dsn,now):
    import asyncpg
    from bot.core.rental_stage2_index import _identity_parts
    ctes,joins=_identity_parts()
    query=ctes+"""SELECT r.id,r.url,r.price,r.area,r.rooms,r.floor,r.floors_total,
        r.address,r.district,r.city,r.prop_type,r.rent_period,r.rent_period_conflict,
        r.is_active,r.archived_at,r.is_duplicate,r.dup_needs_review,r.duplicate_of,
        r.last_seen,r.complex_id,r.photo_url,r.photos,r.rental_episode_started_at,r.rental_episode_last_seen_at,
        identity.id AS index_complex_id,identity.name AS index_complex_name,
        (identity.id IS NOT NULL) AS complex_binding_verified,
        coalesce(proof.observations,'[]'::jsonb) AS observations
        FROM rental_listings r """+joins+"""
        LEFT JOIN LATERAL (
          SELECT jsonb_agg(jsonb_build_object('observed_at',o.observed_at,'price',o.price,
            'episode_started_at',o.episode_started_at,'rent_period',o.rent_period,
            'period_source_checked',o.period_source_checked) ORDER BY o.observed_at) AS observations
          FROM rental_observations o WHERE o.listing_id=r.id AND o.rent_period='monthly'
            AND o.period_source_checked AND o.episode_started_at=r.rental_episode_started_at
            AND o.observed_at BETWEEN $1::timestamptz-interval '90 days' AND $1
        ) proof ON TRUE
        WHERE r.city='astana' AND r.prop_type='apartment' AND r.rent_period='monthly'
          AND NOT coalesce(r.rent_period_conflict,FALSE) AND r.is_active IS TRUE AND r.archived_at IS NULL
          AND NOT coalesce(r.is_duplicate,FALSE) AND NOT coalesce(r.dup_needs_review,FALSE)
          AND nullif(btrim(r.duplicate_of),'') IS NULL
          AND r.last_seen BETWEEN $1::timestamptz-interval '72 hours' AND $1
          AND r.rental_episode_last_seen_at BETWEEN $1::timestamptz-interval '72 hours' AND $1
        ORDER BY r.id LIMIT 20001"""
    conn=await asyncpg.connect(dsn,command_timeout=60,server_settings={
        'default_transaction_read_only':'on','application_name':'hatuli_rental_picks_readonly'})
    try:
        async with conn.transaction(readonly=True,isolation='repeatable_read'):
            if await conn.fetchval('SHOW transaction_read_only')!='on':
                raise RuntimeError('Read-only rental selection required')
            rows=[dict(row) for row in await conn.fetch(query,now)]
            if len(rows)>20000: raise ValueError('Candidate scan exceeds bounded complete population')
            return rows
    finally:
        await conn.close()


def private_json(path,data):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with path.open('w',encoding='utf-8') as output:
        os.chmod(path,0o600)
        json.dump(data,output,ensure_ascii=False,default=str,indent=2,allow_nan=False)


def source_checks(path):
    raw=json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(raw,dict) or raw.get('method')!='normal_browser' or not isinstance(raw.get('items'),list):
        raise ValueError('Ordinary browser source checks required')
    items=raw['items']
    if len(items)>500 or any(not isinstance(item,dict) for item in items):
        raise ValueError('Invalid rental source checks')
    result={str(item.get('id')):item for item in items}
    if len(result)!=len(items): raise ValueError('Duplicate rental source checks')
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=ROOT,help='Existing configuration directory; no pool/startup')
    parser.add_argument('--export-candidates',type=Path)
    parser.add_argument('--source-checks',type=Path)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    if bool(args.export_candidates)==bool(args.source_checks):
        parser.error('Choose private candidate export or checked publication')
    from dotenv import dotenv_values
    dsn=dotenv_values(args.root/'.env',interpolate=False).get('DATABASE_URL') or os.environ.get('DATABASE_URL')
    if not dsn: parser.error('DATABASE_URL required')
    now=datetime.now(timezone.utc)
    rows=asyncio.run(read_candidates(dsn,now))
    checks=source_checks(args.source_checks) if args.source_checks else None
    edition=build_rental_picks(rows,now=now,checks=checks,require_checks=checks is not None)
    if args.export_candidates:
        private_json(args.export_candidates,{'method':'candidate_export_not_verified',**edition})
        print(json.dumps({'read_rows':len(rows),'candidates':len(edition['items']),'database_writes':0}))
    else:
        # A failed or incomplete release leaves the previous file untouched.
        publish_snapshot(edition,args.output or snapshot_path())
        print(json.dumps({'published':len(edition['items']),'selection_date':edition['selection_date'],'database_writes':0}))


if __name__=='__main__': main()
