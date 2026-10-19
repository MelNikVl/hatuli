"""Observed asking rents and source removals; neither bookings nor achieved rents.

Daily history is immutable and starts at migration 112. A current last_seen is
never converted into invented 30/90-day history. Each ID contributes one latest
observation to a price distribution, regardless of how often it was scanned.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import math
import re
import statistics

import asyncpg

from bot.core.rental_furnishing import furnishing_validation_ready, furnishing_index_row_ready, version_tag

DAILY_POLICY = 'daily_v1'
FURNISHED_MONTHLY_POLICY = 'furnished_monthly_v1'
HEX_MONTHLY_POLICY = 'hex_monthly_v1'
TTL_HOURS = 72
PERIODS = {'monthly','daily'}

def utc(value):
    if isinstance(value,str):
        try:
            value=datetime.fromisoformat(value.replace('Z','+00:00'))
        except (ValueError,OverflowError):
            return None
    return value.astimezone(timezone.utc) if isinstance(value,datetime) and value.tzinfo else None


def _valid(row, now, *, historical=False):
    period=row.get('rent_period')
    observed=utc(row.get('observed_at') if historical else row.get('last_seen'))
    limit=90*24 if historical and period=='daily' else 72
    if period=='monthly':
        from bot.core.comparison_rental_estimate import _NONMONTHLY
        if _NONMONTHLY.search(row.get('title') or ''):
            return False
    return (isinstance(row.get('id'),str) and bool(row['id']) and row.get('city')=='astana'
            and row.get('prop_type','apartment')=='apartment' and period in PERIODS
            and row.get('rent_period_conflict') not in (True,)
            and row.get('is_duplicate') not in (True,) and row.get('dup_needs_review') not in (True,)
            and not row.get('duplicate_of') and observed is not None
            and now-timedelta(hours=limit)<=observed<=now
            and type(row.get('rooms')) is int and 1<=row['rooms']<=6
            and type(row.get('price')) is int
            and (50000 if period=='monthly' else 1000)<=row['price']<=2000000
            and (historical and row.get('period_source_checked') is True
                 or not historical and row.get('is_active') is True and row.get('archived_at') is None))


def _scopes(row, furnishing_enabled, furnishing_version=None):
    """Only verified exact non-umbrella identities receive a complex scope."""
    labels=[None]
    if (furnishing_enabled and row.get('furnishing') in ('none','partial','full')
            and row.get('furnishing_version')==furnishing_version):
        labels.append(row['furnishing'])
    cid=row.get('index_complex_id') if row.get('complex_binding_verified') is True else None
    name=row.get('index_complex_name') if cid else None
    district=row.get('district') or None
    for furnished in labels:
        if cid:
            yield ('complex',row['city'],None,name,cid,row['rooms'],row['rent_period'],furnished)
        if district:
            yield ('district',row['city'],district,None,None,row['rooms'],row['rent_period'],furnished)
        yield ('city',row['city'],None,None,None,row['rooms'],row['rent_period'],furnished)
        if (isinstance(row.get('hex_id'),str) and re.fullmatch(r'-?\d+:-?\d+',row['hex_id'])
                and type(row.get('hex_edge_m')) in (int,float) and math.isfinite(row['hex_edge_m'])
                and 25<=row['hex_edge_m']<=500):
            yield ('hex',row['city'],row['hex_id'],float(row['hex_edge_m']),None,row['rooms'],row['rent_period'],furnished)


def _minimum(scope,min_complex,min_area,min_hex):
    return min_hex if scope=='hex' else min_complex if scope=='complex' else min_area


def _distribution(rows, now, period, minimum):
    for days in ((30,90) if period=='daily' else (30,)):
        oldest=now-timedelta(days=days) if period=='daily' else now-timedelta(hours=72)
        latest={}
        for row in sorted(rows,key=lambda item:utc(item.get('observed_at') or item.get('last_seen')),reverse=True):
            observed=utc(row.get('observed_at') or row.get('last_seen'))
            if observed>=oldest:
                latest.setdefault(row['id'],row)
        if len(latest)<minimum:
            continue
        values=list(latest.values())
        times=[utc(row.get('observed_at') or row.get('last_seen')) for row in values]
        if max(times)<now-timedelta(hours=TTL_HOURS):
            continue
        prices=[row['price'] for row in values]
        p25,_,p75=statistics.quantiles(prices,n=4,method='inclusive')
        m2=[row['price']/row['area'] for row in values if type(row.get('area')) in (int,float)
            and math.isfinite(row['area']) and 20<=row['area']<=300]
        return {'median_price':int(statistics.median(prices)),'avg_price':int(statistics.mean(prices)),
                'p25_price':int(p25),'p75_price':int(p75),'sample_count':len(prices),
                'price_per_sqm':int(statistics.median(m2)) if m2 else None,'window_days':days,
                'source_oldest_at':min(times),'source_newest_at':max(times),'updated_at':now}
    return None


def build_stage2_records(observations, live_rows, removed_rows, now, *, min_complex=5,min_area=10,min_hex=5,
                         furnishing_enabled=False,furnishing_version=None):
    """Pure builder. Removal duration is never an occupancy/vacancy input."""
    min_complex,min_area,min_hex=max(5,min_complex),max(10,min_area),max(5,min_hex)
    groups=defaultdict(list)
    active=defaultdict(set)
    removals=defaultdict(dict)
    daily_latest={}
    for raw in observations:
        row=dict(raw)
        if row.get('rent_period')=='daily' and _valid(row,now,historical=True):
            previous=daily_latest.get(row['id'])
            if previous is None or utc(row['observed_at'])>utc(previous['observed_at']):
                daily_latest[row['id']]=row
    for raw in live_rows:
        row=dict(raw)
        if _valid(row,now):
            for key in _scopes(row,furnishing_enabled,furnishing_version):
                active[key].add(row['id'])
                # Monthly prices retain the stage-0 fresh-source semantics.
                if row['rent_period']=='monthly':
                    groups[key].append(row)
            if row['rent_period']=='daily':
                # The immutable ledger keeps the first daily price, while a
                # later successful catalog observation can change today's price.
                # Never substitute generic last_seen or an enrichment time for
                # the source-proved timestamp of the current continuous episode.
                episode=utc(row.get('rental_episode_started_at'))
                observed=utc(row.get('rental_episode_last_seen_at'))
                seen=utc(row.get('last_seen'))
                if episode and observed and episode<=observed<=seen and now-timedelta(hours=72)<=observed<=now:
                    current={**row,'observed_at':observed,'period_source_checked':True,'episode_started_at':episode}
                    previous=daily_latest.get(row['id'])
                    if previous is None or observed>=utc(previous['observed_at']):
                        daily_latest[row['id']]=current
    # Choose one actual latest observation per ID before assigning scope: an
    # updated room/district must not leave the same ID in its former segment.
    for row in daily_latest.values():
        for key in _scopes(row,furnishing_enabled,furnishing_version):
            groups[key].append(row)
    for raw in removed_rows:
        row=dict(raw)
        first,gone=utc(row.get('first_verified_monthly_at')),utc(row.get('archived_at'))
        episode,last_catalog=utc(row.get('rental_episode_started_at')),utc(row.get('rental_episode_last_seen_at'))
        last_proof=utc(row.get('last_verified_monthly_at'))
        if (row.get('city')!='astana' or row.get('prop_type')!='apartment'
                or type(row.get('rooms')) is not int or not 1<=row['rooms']<=6
                or row.get('rent_period')!='monthly' or row.get('rent_period_conflict') is True
                or row.get('is_duplicate') is True or row.get('dup_needs_review') is True or row.get('duplicate_of')
                or row.get('archive_reason') not in ('archived_badge','confirmed_gone')
                or row.get('is_active') is not False or not first or not gone
                or not episode or not last_catalog or not last_proof
                or type(row.get('monthly_proof_days')) is not int or row['monthly_proof_days']<2
                or not episode<=first<=last_proof<=last_catalog<=gone
                or gone-last_proof>timedelta(hours=72)
                or not now-timedelta(days=30)<=gone<=now or (gone-first).total_seconds()<2*86400):
            continue
        for key in _scopes(row,furnishing_enabled,furnishing_version):
            removals[key][row['id']]=(gone-first).total_seconds()/86400
    records=[]
    for key,rows in groups.items():
        scope,city,district,name,cid,rooms,period,furnished=key
        minimum=_minimum(scope,min_complex,min_area,min_hex)
        values=_distribution(rows,now,period,minimum)
        if values is None or not active[key]:
            continue
        gone=list(removals[key].values())
        removal_ready=period=='monthly' and len(gone)>=minimum
        values.update(scope=scope,city=city,district=district,complex_name=name,complex_id=cid,rooms=rooms,
                      rent_period=period,furnishing=furnished,active_supply=len(active[key]),
                      furnishing_version=furnishing_version if furnished else None,
                      median_dom_days=round(statistics.median(gone),1) if removal_ready else None,
                      gone_30d=len(gone) if removal_ready else None,
                      months_of_supply=round(len(active[key])/len(gone),2) if removal_ready else None,
                      policy_version=DAILY_POLICY if period=='daily' else
                        HEX_MONTHLY_POLICY if scope=='hex' else FURNISHED_MONTHLY_POLICY)
        if scope=='hex':
            values.update(hex_id=district,hex_edge_m=name,district=None,complex_name=None)
        records.append(values)
    return records


def _identity_parts():
    # Reuse the exact stage-0 source/canonical/name evidence gates, not a second
    # interpretation of umbrellas or guessed names. The remaining SQL is ours.
    from bot.core.rental_parser import _RENTAL_INDEX_ROWS_SQL
    ctes=_RENTAL_INDEX_ROWS_SQL.split('SELECT r.id,r.city',1)[0]
    joins="""
LEFT JOIN cx bound ON bound.id=r.complex_id
LEFT JOIN unique_names named ON r.complex_id IS NULL AND named.n=complex_name_key(r.complex_name)
LEFT JOIN unique_sources source ON r.complex_url ~ '^https://krisha[.]kz/complex/show/[^/?#]+(/[^/?#]+)?/?$'
 AND source.url=rtrim(r.complex_url,'/')
LEFT JOIN complexes identity ON identity.id=coalesce(bound.cid,named.cid)
 AND NOT coalesce(identity.is_garbage,FALSE) AND NOT coalesce(identity.is_street,FALSE)
 AND NOT coalesce(identity.is_umbrella,FALSE) AND identity.canonical_reason IS DISTINCT FROM 'junk_unmatched'
 AND (source.cid=identity.id OR (r.complex_id IS NOT NULL AND source.cid=identity.parent_complex_id))
 AND NOT EXISTS(SELECT 1 FROM names conflicting WHERE conflicting.n=complex_name_key(r.complex_name)
   AND conflicting.cid<>identity.id AND conflicting.cid IS DISTINCT FROM identity.parent_complex_id)
"""
    return ctes,joins


async def _source_rows(connection,now):
    ctes,joins=_identity_parts()
    identity="identity.id AS index_complex_id,identity.name AS index_complex_name,(identity.id IS NOT NULL) AS complex_binding_verified"
    clean="""r.city='astana' AND r.prop_type='apartment' AND r.rent_period IN ('daily','monthly')
 AND NOT coalesce(r.rent_period_conflict,FALSE) AND NOT coalesce(r.is_duplicate,FALSE)
 AND NOT coalesce(r.dup_needs_review,FALSE) AND nullif(btrim(r.duplicate_of),'') IS NULL"""
    columns=','.join('r.'+name for name in ('id','city','district','rooms','prop_type','rent_period','rent_period_conflict','title',
        'price','area','last_seen','is_active','archived_at','is_duplicate','dup_needs_review','duplicate_of',
        'furnishing','furnishing_version','hex_id','hex_edge_m','found_at','archive_reason',
        'rental_episode_started_at','rental_episode_last_seen_at'))
    live=await connection.fetch(ctes+f"SELECT {columns},{identity} FROM rental_listings r "+joins+f"WHERE {clean} "
        "AND r.is_active IS TRUE AND r.archived_at IS NULL AND r.last_seen BETWEEN $1::timestamptz-interval '72 hours' AND $1",now)
    episode_proof=""" JOIN LATERAL (
      SELECT min(o.observed_at) AS first_verified_monthly_at,max(o.observed_at) AS last_verified_monthly_at,
             count(DISTINCT o.observed_on)::integer AS monthly_proof_days
      FROM rental_observations o WHERE o.listing_id=r.id AND o.rent_period='monthly' AND o.period_source_checked
        AND o.episode_started_at=r.rental_episode_started_at
        AND o.observed_at BETWEEN r.rental_episode_started_at AND r.rental_episode_last_seen_at
    ) proof ON proof.monthly_proof_days>=2 """
    removed=await connection.fetch(ctes+f"SELECT {columns},{identity},proof.* FROM rental_listings r "+joins+episode_proof+f"WHERE {clean} "
        "AND r.rent_period='monthly' AND r.is_active IS FALSE AND r.archive_reason IN ('archived_badge','confirmed_gone') "
        "AND r.archived_at BETWEEN $1::timestamptz-interval '30 days' AND $1 "
        "AND r.archived_at>=proof.first_verified_monthly_at+interval '2 days' "
        "AND proof.last_verified_monthly_at BETWEEN r.archived_at-interval '72 hours' AND r.archived_at "
        "AND r.rental_episode_last_seen_at<=r.archived_at",now)
    # Current flags veto historical observations subsequently identified as a
    # duplicate or conflicting unit. Old observations themselves stay immutable.
    history_ctes=ctes.rstrip()+""", recent_daily AS (
  SELECT DISTINCT ON (o.listing_id) o.* FROM rental_observations o
  WHERE o.city='astana' AND o.rent_period='daily' AND o.period_source_checked
    AND o.observed_at BETWEEN $1::timestamptz-interval '90 days' AND $1
  ORDER BY o.listing_id,o.observed_at DESC
)\n"""
    history=await connection.fetch(history_ctes+f"""
SELECT r.*,r.listing_id AS id,{identity},FALSE AS rent_period_conflict
FROM recent_daily r JOIN rental_listings current_listing ON current_listing.id=r.listing_id
"""+joins+"""WHERE r.city='astana' AND r.rent_period='daily' AND r.period_source_checked
 AND r.observed_at BETWEEN $1::timestamptz-interval '90 days' AND $1
 AND current_listing.prop_type='apartment' AND current_listing.city='astana' AND current_listing.rent_period=r.rent_period
 AND NOT coalesce(current_listing.rent_period_conflict,FALSE) AND NOT coalesce(current_listing.is_duplicate,FALSE)
 AND NOT coalesce(current_listing.dup_needs_review,FALSE) AND nullif(btrim(current_listing.duplicate_of),'') IS NULL
""",now)
    return history,live,removed


async def rebuild_rental_stage2_index(connection,now):
    """Use the monthly builder's transaction; a failure rolls both scopes back."""
    from bot.core.rental_parser import _rental_index_thresholds
    from bot.db import settings
    await connection.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))",'rental-index-stage2')
    values={row['key']:row['value'] for row in await connection.fetch(
        "SELECT key,value FROM app_settings WHERE key LIKE 'RENTAL_FURNISHING_%'")}
    history,live,removed=await _source_rows(connection,now)
    mc,ma=_rental_index_thresholds()
    try:
        mh=max(5,min(10000,int(settings.get('RENT_MIN_SAMPLE_HEX','5'))))
    except (ValueError,TypeError,OverflowError):
        mh=5
    records=build_stage2_records(history,live,removed,now,min_complex=mc,min_area=ma,min_hex=mh,
                                 furnishing_enabled=furnishing_validation_ready(values),
                                 furnishing_version=version_tag(values.get('RENTAL_FURNISHING_RULES_JSON','{}')))
    await connection.execute("DELETE FROM rental_index i WHERE i.city='astana' AND i.prop_type='apartment' "
        "AND (i.rent_period='daily' OR (i.rent_period='monthly' AND i.furnishing IS NOT NULL))")
    await connection.execute("DELETE FROM rental_hex_index h WHERE h.city='astana'")
    for row in records:
        if row['scope']=='hex':
            await connection.execute("""INSERT INTO rental_hex_index
              (city,hex_id,hex_edge_m,rooms,rent_period,furnishing,median_price,p25_price,p75_price,
               sample_count,active_supply,window_days,source_oldest_at,source_newest_at,updated_at,policy_version,furnishing_version)
              VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)""",
              *[row[key] for key in ('city','hex_id','hex_edge_m','rooms','rent_period','furnishing','median_price',
                'p25_price','p75_price','sample_count','active_supply','window_days','source_oldest_at','source_newest_at','updated_at','policy_version','furnishing_version')])
        elif row['rent_period']=='monthly' and row['furnishing'] is None:
            # Preserve stage-0 policy/price fields consumed by stage 1.
            await connection.execute("""UPDATE rental_index i SET median_dom_days=$1,gone_30d=$2,months_of_supply=$3,
                active_supply=$4 WHERE i.city=$5 AND i.district IS NOT DISTINCT FROM $6::text
                AND i.complex_id IS NOT DISTINCT FROM $7::integer AND i.rooms=$8
                AND i.prop_type='apartment' AND i.rent_period='monthly' AND i.furnishing IS NULL AND i.policy_version='3'""",
                row['median_dom_days'],row['gone_30d'],row['months_of_supply'],row['active_supply'],
                row['city'],row['district'],row['complex_id'],row['rooms'])
        else:
            columns=('city','district','complex_name','complex_id','rooms','rent_period','furnishing','median_price','avg_price',
                'p25_price','p75_price','sample_count','price_per_sqm','updated_at','window_days','active_supply',
                'source_oldest_at','source_newest_at','policy_version','median_dom_days','gone_30d','months_of_supply','furnishing_version')
            await connection.execute('INSERT INTO rental_index('+','.join(columns)+') VALUES ('+
                ','.join(f'${i}' for i in range(1,len(columns)+1))+')',*[row[key] for key in columns])
    return {'groups':len(records),'daily_groups':sum(r['rent_period']=='daily' and r['scope']!='hex' for r in records),
            'furnishing_validation_ready':furnishing_validation_ready(values),'observation_rows':len(history)}


def index_ready(row,now,minimum,period,*,policy=None):
    if not row:
        return False
    updated,oldest,newest=(utc(row.get(key)) for key in ('updated_at','source_oldest_at','source_newest_at'))
    window=row.get('window_days')
    prices=[row.get(key) for key in ('p25_price','median_price','p75_price')]
    return (row.get('city')=='astana' and type(row.get('rooms')) is int and 1<=row['rooms']<=6
            and row.get('rent_period')==period and row.get('policy_version')==policy
            and window in ((30,90) if period=='daily' else (30,))
            and type(row.get('sample_count')) is int and row['sample_count']>=minimum
            and type(row.get('active_supply')) is int and row['active_supply']>0
            and all(type(value) is int and (1000 if period=='daily' else 50000)<=value<=2000000 for value in prices)
            and prices[0]<=prices[1]<=prices[2] and None not in (updated,oldest,newest)
            and now-timedelta(hours=72)<=newest<=updated<=now
            and now-timedelta(days=window)<=oldest<=newest
            and (period=='daily' or oldest>=now-timedelta(hours=72)))


async def _furnishing_values():
    from bot.db.pg import fetch
    values={row['key']:row['value'] for row in await fetch("SELECT key,value FROM app_settings WHERE key LIKE 'RENTAL_FURNISHING_%'")}
    return values


async def _lookup(city,district,complex_name,rooms,*,complex_id,furnishing,allow_city,period,policy):
    if city!='astana' or type(rooms) is not int or not 1<=rooms<=6 or furnishing not in (None,'none','partial','full'):
        return None
    from bot.core.rental_parser import _extract_district,_rental_index_thresholds
    from bot.db.pg import fetchrow
    mc,ma=_rental_index_thresholds()
    district=_extract_district(district or '') or district or None
    now=datetime.now(timezone.utc)
    try:
        furnishing_values=await _furnishing_values() if furnishing else {}
        if furnishing and not furnishing_validation_ready(furnishing_values):
            return None
        attempts=[]
        if type(complex_id) is int and complex_id>0:
            attempts.append((None,complex_id,'ЖК',mc))
        # Name-only scope is deliberately not invented by the new reader.
        if district:
            attempts.append((district,None,'район',ma))
        if allow_city:
            attempts.append((None,None,'город',ma))
        for d,cid,level,minimum in attempts:
            row=await fetchrow("""SELECT i.* FROM rental_index i
              LEFT JOIN complexes c ON c.id=i.complex_id
              WHERE i.city=$1 AND i.district IS NOT DISTINCT FROM $2::text
                AND i.complex_id IS NOT DISTINCT FROM $3::integer AND i.rooms=$4
                AND i.prop_type='apartment' AND i.rent_period=$5 AND i.furnishing IS NOT DISTINCT FROM $6::text
                AND i.policy_version=$7
                AND ($3::integer IS NULL AND i.complex_name IS NULL OR $3::integer IS NOT NULL
                  AND c.canonical_id IS NULL AND NOT coalesce(c.is_umbrella,FALSE)
                  AND NOT coalesce(c.is_garbage,FALSE) AND NOT coalesce(c.is_street,FALSE)
                  AND c.canonical_reason IS DISTINCT FROM 'junk_unmatched')""",city,d,cid,rooms,period,furnishing,policy)
            result=dict(row) if row else None
            if (index_ready(result,now,minimum,period,policy=policy)
                    and (not furnishing or furnishing_index_row_ready(result,furnishing_values))):
                result['level']=level
                return result
    except (asyncpg.UndefinedTableError,asyncpg.UndefinedColumnError):
        return None
    return None


async def lookup_daily_rental_estimate(city,district,complex_name,rooms,*,complex_id=None,furnishing=None,allow_city=False):
    result=await _lookup(city,district,complex_name,rooms,complex_id=complex_id,furnishing=furnishing,
                         allow_city=allow_city,period='daily',policy=DAILY_POLICY)
    if result is None and furnishing in ('none','partial','full'):
        result=await _lookup(city,district,complex_name,rooms,complex_id=complex_id,furnishing=None,
                             allow_city=allow_city,period='daily',policy=DAILY_POLICY)
    return result


async def lookup_furnished_monthly_estimate(city,district,complex_name,rooms,*,complex_id=None,furnishing='full',allow_city=False):
    if furnishing is None:
        return None
    return await _lookup(city,district,complex_name,rooms,complex_id=complex_id,furnishing=furnishing,
                         allow_city=allow_city,period='monthly',policy=FURNISHED_MONTHLY_POLICY)


async def _lookup_hex_exact(hex_id,rooms,*,rent_period='daily',hex_edge_m=None,furnishing=None):
    if (rent_period not in PERIODS or type(rooms) is not int or not 1<=rooms<=6
            or not isinstance(hex_id,str) or not re.fullmatch(r'-?\d+:-?\d+',hex_id)):
        return None
    from bot.db import settings
    from bot.db.pg import fetchrow
    try:
        edge=float(hex_edge_m if hex_edge_m is not None else settings.get('HEX_EDGE_M','50'))
        if not math.isfinite(edge) or not 25<=edge<=500 or furnishing not in (None,'none','partial','full'):
            return None
        furnishing_values=await _furnishing_values() if furnishing else {}
        if furnishing and not furnishing_validation_ready(furnishing_values):
            return None
        row=await fetchrow("SELECT h.* FROM rental_hex_index h WHERE h.city='astana' AND h.hex_edge_m=$1 "
            "AND h.hex_id=$2 AND h.rooms=$3 AND h.rent_period=$4 AND h.furnishing IS NOT DISTINCT FROM $5::text",
            edge,hex_id,rooms,rent_period,furnishing)
        result=dict(row) if row else None
        minimum=max(5,int(settings.get('RENT_MIN_SAMPLE_HEX','5')))
        policy=DAILY_POLICY if rent_period=='daily' else HEX_MONTHLY_POLICY
        if (index_ready(result,datetime.now(timezone.utc),minimum,rent_period,policy=policy)
                and (not furnishing or furnishing_index_row_ready(result,furnishing_values))):
            result['level']='гекс'
            return result
    except (ValueError,TypeError,OverflowError,asyncpg.UndefinedTableError,asyncpg.UndefinedColumnError):
        return None
    return None


async def lookup_hex_rental_estimate(hex_id,rooms,*,rent_period='daily',hex_edge_m=None,furnishing=None):
    result=await _lookup_hex_exact(hex_id,rooms,rent_period=rent_period,hex_edge_m=hex_edge_m,furnishing=furnishing)
    if result is None and rent_period=='daily' and furnishing in ('none','partial','full'):
        result=await _lookup_hex_exact(hex_id,rooms,rent_period=rent_period,hex_edge_m=hex_edge_m,furnishing=None)
    return result
