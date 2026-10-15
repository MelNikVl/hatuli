"""Actual PG contracts, opt-in only; all synthetic seeds roll back.

Migrations must already exist. No pool initialization/migration execution.
Use the stage-0 explicit local test-DB opt-in, and run serially without xdist.
These synthetic database fixtures are not real-source furnishing validation.
"""
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4
import os

import asyncpg
import pytest
import pytest_asyncio

from tests.test_rental_stage0_pg import OPT_IN,_validated_test_dsn,_raw_connect


@pytest_asyncio.fixture
async def rental_stage2_pg(monkeypatch):
    supplied=os.environ.get(OPT_IN)
    if not supplied:
        pytest.skip(f'Set {OPT_IN} to run PostgreSQL stage2 checks')
    try:
        dsn=_validated_test_dsn(supplied)
    except ValueError:
        pytest.fail('Unsafe opt-in PostgreSQL destination; no connection attempted',pytrace=False)
    monkeypatch.setattr('dotenv.load_dotenv',lambda *a,**k:False)
    from bot.db import pg,settings
    def deny(*args,**kwargs):
        raise AssertionError('No application pools or migrations in this PG fixture')
    monkeypatch.setattr(pg,'init_pool',deny)
    monkeypatch.setattr(asyncpg,'create_pool',deny)
    connection=await _raw_connect(dsn,timeout=5,command_timeout=20,
        server_settings={'application_name':'hatuli-stage2-pg-test','lock_timeout':'2000','statement_timeout':'20000'})
    transaction=connection.transaction(isolation='repeatable_read')
    started=False
    try:
        assert await connection.fetchval('SELECT current_database()')=='hatuli_test'
        await transaction.start();started=True
        assert await connection.fetchval("SELECT to_regclass('rental_observations')") is not None, 'Apply migration112 externally'
        for name in ('fetch','fetchrow','fetchval','execute'):
            monkeypatch.setattr(pg,name,getattr(connection,name))
        monkeypatch.setattr(settings,'get',lambda key,default=None:default)
        monkeypatch.setattr(settings,'get_int',lambda key,default=0:default)
        await connection.execute("INSERT INTO app_settings(key,value) VALUES('RENTAL_FURNISHING_INDEX_ENABLED','0') "
                                  "ON CONFLICT(key) DO UPDATE SET value='0'")
        yield SimpleNamespace(connection=connection,now=await connection.fetchval('SELECT clock_timestamp()'),
                              prefix='~rental_stage2_pg_'+uuid4().hex+'_')
    finally:
        try:
            if started:await transaction.rollback()
        finally:
            await connection.close()


@pytest.mark.asyncio
async def test_112_real_catalog_snapshot_is_once_per_id_day_and_unit(rental_stage2_pg,monkeypatch):
    from bot.core import rental_parser as parser
    state=rental_stage2_pg
    clock={'now':state.now}
    monkeypatch.setattr(parser,'datetime',SimpleNamespace(now=lambda tz:clock['now']))
    def card(suffix,period,price):
        return parser.RentalListing(state.prefix+suffix,'https://krisha.kz/a/show/123456','Квартира',price,50,2,3,9,
            'Есиль','Есиль','','astana','apartment','',rent_period=period)
    assert await parser.save_rental_listings([card('daily','daily',20000),card('monthly','monthly',250000),card('unknown','unknown',300000)])==3
    assert await parser.save_rental_listings([card('daily','daily',22000)])==1
    rows=await state.connection.fetch("SELECT listing_id,rent_period,price FROM rental_observations "
        "WHERE listing_id LIKE $1 ORDER BY rent_period",state.prefix+'%')
    assert len(rows)==2 and {row['rent_period'] for row in rows}=={'daily','monthly'}
    assert next(row['price'] for row in rows if row['rent_period']=='daily')==20000
    assert await state.connection.fetchval('SELECT price FROM rental_listings WHERE id=$1',state.prefix+'daily')==22000
    await state.connection.execute("INSERT INTO rental_listings(id,rent_period,found_at,last_seen) "
        "VALUES($1,'monthly',$2::timestamptz-interval '100 days',$2::timestamptz-interval '1 hour')",state.prefix+'legacy',state.now)
    assert await parser.save_rental_listings([card('legacy','monthly',250000)])==1
    legacy=await state.connection.fetchrow('SELECT found_at,rental_episode_started_at FROM rental_listings WHERE id=$1',state.prefix+'legacy')
    assert legacy['found_at']<state.now-timedelta(days=90) and legacy['rental_episode_started_at']==state.now
    clock['now']=state.now+timedelta(days=1)
    await parser.save_rental_listings([card('daily','daily',22000)])
    assert await state.connection.fetchval('SELECT rental_episode_started_at FROM rental_listings WHERE id=$1',state.prefix+'daily')==state.now
    clock['now']=state.now+timedelta(days=5)
    await parser.save_rental_listings([card('daily','daily',22000)])
    assert await state.connection.fetchval('SELECT rental_episode_started_at FROM rental_listings WHERE id=$1',state.prefix+'daily')==clock['now']
    await state.connection.execute("UPDATE rental_listings SET is_active=FALSE,archived_at=$2 WHERE id=$1",state.prefix+'daily',clock['now'])
    clock['now']+=timedelta(hours=2)
    await parser.save_rental_listings([card('daily','daily',22000)])
    assert await state.connection.fetchval('SELECT rental_episode_started_at FROM rental_listings WHERE id=$1',state.prefix+'daily')==clock['now']
    assert await state.connection.fetchval('SELECT count(*) FROM rental_observations WHERE listing_id=$1 AND episode_started_at=$2',
        state.prefix+'daily',clock['now'])<=1  # cannot reuse the prior episode's proof
    await parser.save_rental_listings([card('legacy','daily',20000)])
    assert await state.connection.fetchval('SELECT rental_episode_started_at FROM rental_listings WHERE id=$1',state.prefix+'legacy') is None


@pytest.mark.asyncio
async def test_112_real_checks_unknown_period_and_furniture(rental_stage2_pg):
    state=rental_stage2_pg
    with pytest.raises(asyncpg.CheckViolationError):
        async with state.connection.transaction():
            await state.connection.execute("INSERT INTO rental_listings(id,furnishing) VALUES($1,'luxury')",state.prefix)
    with pytest.raises(asyncpg.CheckViolationError):
        async with state.connection.transaction():
            await state.connection.execute("""INSERT INTO rental_observations
              (listing_id,observed_at,observed_on,source,rent_period,period_source_checked,episode_started_at,city,rooms,price,furnishing)
              VALUES($1,$2,($2::timestamptz AT TIME ZONE 'UTC')::date,'krisha','unknown',TRUE,$2,'astana',2,20000,'unknown')""",state.prefix,state.now)


@pytest.mark.asyncio
async def test_112_real_history_90_day_index_hex_window_and_lookup(rental_stage2_pg):
    from bot.core.rental_stage2_index import rebuild_rental_stage2_index,lookup_daily_rental_estimate,lookup_hex_rental_estimate
    state=rental_stage2_pg
    district=state.prefix+'district'
    for i in range(10):
        lid=state.prefix+str(i)
        await state.connection.execute("""INSERT INTO rental_listings
          (id,city,district,rooms,price,area,rent_period,last_seen,hex_id,hex_edge_m)
          VALUES($1,'astana',$3,2,20000,50,'daily',$2,'0:0',125.5)""",lid,state.now,district)
        observed=state.now-timedelta(hours=1) if i<5 else state.now-timedelta(days=45)
        await state.connection.execute("""INSERT INTO rental_observations
          (listing_id,observed_at,observed_on,source,rent_period,period_source_checked,episode_started_at,city,district,rooms,
           price,area,hex_id,hex_edge_m,furnishing)
          VALUES($1,$2,($2::timestamptz AT TIME ZONE 'UTC')::date,'krisha','daily',TRUE,$2,'astana',$3,2,
                 20000,50,'0:0',125.5,'unknown')""",lid,observed,district)
    await rebuild_rental_stage2_index(state.connection,state.now)
    area=await lookup_daily_rental_estimate('astana',district,None,2)
    assert area['window_days']==90 and area['sample_count']>=10 and area['active_supply']>=10
    # Hex needs five, so its genuine recent sample does not need the 90d fallback.
    cell=await lookup_hex_rental_estimate('0:0',2,hex_edge_m=125.5)
    assert cell['window_days']==30 and cell['sample_count']>=5 and cell['median_price']==20000
    assert not await state.connection.fetchval("SELECT EXISTS(SELECT 1 FROM rental_index i WHERE "
        "i.policy_version='furnished_monthly_v1')")
