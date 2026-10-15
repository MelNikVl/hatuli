"""Opt-in checks against externally migrated LOCAL hatuli_test, never production.

No migrations/pool startup and no Telegram. One raw connection per test uses
an outer rollback. Real concurrent connection scheduling is not simulated by
these savepoint tests; the isolated outbox tests additionally exercise the
concurrent algorithm with a shared lock/ledger.
"""
import os
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from uuid import uuid4
from unittest.mock import AsyncMock

import asyncpg
import pytest
import pytest_asyncio

OPT_IN="HATULI_RENTAL_STAGE1_TEST_DATABASE_URL"


def validated_test_dsn(value):
    try:
        parsed=urlsplit(value)
        valid=(isinstance(value,str) and value==value.strip() and not any(ord(c)<32 for c in value)
               and parsed.scheme in ("postgres","postgresql") and parsed.hostname in ("127.0.0.1","localhost")
               and parsed.port in (5432,15439) and parsed.path=="/hatuli_test" and not parsed.query and not parsed.fragment)
    except (ValueError,TypeError,AttributeError): valid=False
    if not valid: raise ValueError("Explicit local hatuli_test required")
    return value


@pytest_asyncio.fixture
async def database(monkeypatch):
    value=os.environ.get(OPT_IN)
    if not value: pytest.skip("Explicit local Stage 1 PostgreSQL opt-in not set")
    dsn=validated_test_dsn(value)
    monkeypatch.setenv("DATABASE_URL","postgresql://unused:unused@127.0.0.1:1/rental_stage1_test")
    monkeypatch.setattr("dotenv.load_dotenv",lambda *a,**k:False)
    denied=AsyncMock(side_effect=AssertionError("No application pool or migrations"))
    from bot.db import pg
    monkeypatch.setattr(pg,"init_pool",denied); monkeypatch.setattr(pg,"_apply_migrations",denied)
    monkeypatch.setattr(asyncpg,"create_pool",denied)
    connection=await asyncpg.connect(dsn,timeout=5,command_timeout=15,
        server_settings={"application_name":"hatuli-rental-stage1-pg-test","lock_timeout":"2000","statement_timeout":"15000"})
    outer=connection.transaction(isolation="repeatable_read")
    started=False
    try:
        assert await connection.fetchval("SELECT current_database()") == "hatuli_test"
        assert connection.get_server_version().major>=15
        await outer.start(); started=True
        assert await connection.fetchval("SELECT to_regclass('rental_yield_benchmarks') IS NOT NULL"), "Apply 111 externally before opting in"
        for name in ("fetch","fetchrow","fetchval","execute"):
            monkeypatch.setattr(pg,name,getattr(connection,name))
        class Pool:
            @asynccontextmanager
            async def acquire(self): yield connection
        monkeypatch.setattr(pg,"get_pool",lambda:Pool())
        yield connection
    finally:
        if started: await outer.rollback()
        await connection.close()


def test_pg_guard_accepts_only_explicit_test_destination():
    assert validated_test_dsn("postgresql://test:test@localhost:15439/hatuli_test")


@pytest.mark.parametrize("value",[None,"", "postgresql://localhost/hatuli_test", "postgresql://localhost:5432/production",
    "postgresql://192.168.0.15:5432/hatuli_test","postgresql://localhost:5432/hatuli_test?host=remote",
    "postgresql://localhost:5432/%68atuli_test","postgresql://localhost:5432/hatuli_test#x"])
def test_pg_guard_rejects_ambient_remote_override(value):
    with pytest.raises(ValueError): validated_test_dsn(value)


async def inserted_sale(connection):
    from bot.core.rental_economics import build_rental_economics
    from bot.core.rental_yield import NetYieldParams
    identifier="9"+str(uuid4().int)[:14]
    await connection.execute("""INSERT INTO apartment_listings(id,price,area,rooms,is_active,last_seen,first_seen)
        VALUES($1,40000000,60,2,TRUE,now()-interval '5 minutes',now()-interval '3 days')""",identifier)
    row=dict(await connection.fetchrow("SELECT * FROM apartment_listings WHERE id=$1",identifier))
    now=await connection.fetchval("SELECT now()")
    ix={"level":"район","median_price":300000,"p25_price":250000,"p75_price":350000,"price_per_sqm":5000,
        "sample_count":10,"rent_period":"monthly","policy_version":"3","window_days":30,
        "updated_at":now-timedelta(minutes=1),"source_oldest_at":now-timedelta(hours=3),"source_newest_at":now-timedelta(hours=1)}
    row["rental_economics"]=build_rental_economics(row,ix,NetYieldParams(),now=now)
    await connection.execute("UPDATE apartment_listings SET rental_economics=$2::jsonb WHERE id=$1",identifier,json.dumps(row["rental_economics"]))
    return row


@pytest.mark.asyncio
async def test_real_snapshot_sql_matches_ready_then_rejects_price_drift_and_stale_source(database):
    from bot.core.rental_economics_filters import append_rental_filters
    row=await inserted_sale(database)
    conds,params=[],[row["id"]]
    append_rental_filters(conds,params,net_yield_min=7)
    sql="SELECT a.id FROM apartment_listings a WHERE a.id=$1 "+" ".join(conds)
    assert await database.fetchval(sql,*params)==row["id"]
    await database.execute("UPDATE apartment_listings SET price=41000000 WHERE id=$1",row["id"])
    assert await database.fetchval(sql,*params) is None
    await database.execute("UPDATE apartment_listings SET price=40000000,rental_economics=jsonb_set(rental_economics,'{rent_source,oldest_epoch}',to_jsonb(extract(epoch FROM now())-300000)) WHERE id=$1",row["id"])
    assert await database.fetchval(sql,*params) is None


@pytest.mark.asyncio
async def test_real_snapshot_sql_malformed_numeric_json_fails_closed(database):
    from bot.core.rental_economics_filters import append_rental_filters
    row=await inserted_sale(database)
    conds,params=[],[row["id"]]
    append_rental_filters(conds,params,net_yield_min=7)
    sql="SELECT a.id FROM apartment_listings a WHERE a.id=$1 "+" ".join(conds)
    await database.execute("UPDATE apartment_listings SET rental_economics=jsonb_set(rental_economics,'{rent_source,n}','\"bad\"'::jsonb) WHERE id=$1",row["id"])
    assert await database.fetchval(sql,*params) is None


@pytest.mark.asyncio
async def test_real_snapshot_sql_rejects_district_correction_and_primary(database):
    from bot.core.rental_economics_filters import append_rental_filters
    row=await inserted_sale(database)
    conds,params=[],[row["id"]]
    append_rental_filters(conds,params,net_yield_min=7)
    sql="SELECT a.id FROM apartment_listings a WHERE a.id=$1 "+" ".join(conds)
    assert await database.fetchval(sql,*params)==row["id"]
    await database.execute("UPDATE apartment_listings SET district='Другой район' WHERE id=$1",row["id"])
    assert await database.fetchval(sql,*params) is None
    await database.execute("UPDATE apartment_listings SET district=$2,market_type='primary' WHERE id=$1",row["id"],row["district"])
    assert await database.fetchval(sql,*params) is None


@pytest.mark.asyncio
async def test_real_typed_search_property_unique_and_daily_cap(database,monkeypatch):
    from bot.core.buyer_alerts import enqueue_typed_candidate
    row=await inserted_sale(database)
    user_id=-int(str(uuid4().int)[:12])
    await database.execute("INSERT INTO users(user_id,notify_frequency) VALUES($1,'daily')",user_id)
    await database.execute("INSERT INTO saved_searches(user_id,kind,params) VALUES($1,'yield','{\"net_yield_min\":7}')",user_id)
    # Isolate this test subscription from any unrelated local test fixtures.
    original=database.fetch
    async def fetch(sql,*args):
        rows=await original(sql,*args)
        return [r for r in rows if r.get("user_id")==user_id] if "FROM saved_searches s JOIN users" in sql else rows
    monkeypatch.setattr("bot.db.pg.fetch",fetch)
    monkeypatch.setattr("bot.db.settings.get_int",lambda key,default:1 if key=="BUYER_ALERT_DAILY_CAP" else default)
    row.update(property_id=123456789,property_identity_status="confirmed")
    assert (await enqueue_typed_candidate("yield",row))["queued"]==1
    assert (await enqueue_typed_candidate("yield",row))["queued"]==0
    row.update(property_id=123456790)
    assert (await enqueue_typed_candidate("yield",row))["queued"]==0
    assert await database.fetchval("SELECT count(*) FROM user_listing_notifications WHERE user_id=$1",user_id)==1


@pytest.mark.asyncio
async def test_real_benchmark_unique_nullable_key_and_savepoint_rollback(database):
    from bot.core.rental_yield import NetYieldParams
    from dataclasses import asdict
    key="stage1-test-"+uuid4().hex
    async with database.transaction():
        await database.execute("""INSERT INTO rental_yield_benchmarks(scope,scope_value,rooms,median_net,p90_net,n,net_values,
            computed_at,source_oldest_at,source_newest_at,params_used,policy_version)
            VALUES('district',$1,2,7,7,10,ARRAY[7,7,7,7,7,7,7,7,7,7]::double precision[],now(),now()-interval '3 hours',now()-interval '1 hour',$2::jsonb,'3')""",key,json.dumps(asdict(NetYieldParams())))
        with pytest.raises(asyncpg.UniqueViolationError):
            async with database.transaction():
                await database.execute("INSERT INTO rental_yield_benchmarks SELECT scope,scope_value,rooms,median_net,p90_net,n,net_values,computed_at,source_oldest_at,source_newest_at,params_used,policy_version FROM rental_yield_benchmarks WHERE scope_value=$1",key)
        assert await database.fetchval("SELECT count(*) FROM rental_yield_benchmarks WHERE scope_value=$1",key)==1


async def typed_user(connection):
    user_id=-int(str(uuid4().int)[:12])
    await connection.execute("INSERT INTO users(user_id,notify_frequency) VALUES($1,'daily')",user_id)
    sid=await connection.fetchval("INSERT INTO saved_searches(user_id,kind,params) VALUES($1,'yield','{\"net_yield_min\":7}') RETURNING id",user_id)
    return user_id,sid


@pytest.mark.asyncio
async def test_real_keyset_reaches_201st_even_when_first_200_do_not_qualify(database,monkeypatch):
    from bot.core import buyer_alerts as alerts
    await typed_user(database)
    pid=await database.fetchval("INSERT INTO properties(address_hash,identity_status) VALUES($1,'confirmed') RETURNING property_id","stage1-scan-"+uuid4().hex)
    prefix=str(uuid4().int)[:10]
    ids=[prefix+str(i).zfill(5) for i in range(201)]
    await database.executemany("INSERT INTO apartment_listings(id,price,area,rooms,is_active,last_seen,first_seen) VALUES($1,40000000,60,2,TRUE,now()-interval '5 minutes',now()-interval '1 hour')",[(lid,) for lid in ids])
    await database.executemany("INSERT INTO property_listings(property_id,listing_id,link_method) VALUES($1,$2,'manual')",[(pid,lid) for lid in ids])
    await database.execute("DELETE FROM app_settings WHERE key=$1",alerts.YIELD_SCAN_CURSOR_KEY)
    class Scoped:
        def __getattr__(self,name): return getattr(database,name)
        async def fetch(self,sql,*args):
            if "ORDER BY a.last_seen,a.id" in sql:
                sql=sql.replace("WHERE a.is_active IS TRUE",f"WHERE pl.property_id={int(pid)} AND a.is_active IS TRUE")
            return await database.fetch(sql,*args)
    class Pool:
        @asynccontextmanager
        async def acquire(self): yield Scoped()
    monkeypatch.setattr("bot.db.pg.get_pool",lambda:Pool())
    visited=[]
    async def absent(row): visited.append(row["id"]); return None
    monkeypatch.setattr("bot.core.rental_economics.rental_economics_for_listing",absent)
    first=await alerts.enqueue_recent_yield_candidates(limit=200)
    assert first=={"scanned":200,"queued":0,"has_more":True}
    assert json.loads(await database.fetchval("SELECT value FROM app_settings WHERE key=$1",alerts.YIELD_SCAN_CURSOR_KEY))["after_id"]==ids[199]
    second=await alerts.enqueue_recent_yield_candidates(limit=200)
    assert second=={"scanned":1,"queued":0,"has_more":False}
    assert visited==ids


@pytest.mark.asyncio
async def test_real_midnight_backlog_reserves_delivery_cap_and_defers_remainder(database,monkeypatch):
    from bot.core import buyer_alerts as alerts
    user_id,sid=await typed_user(database)
    for i in range(10):
        row=await inserted_sale(database)
        pid=await database.fetchval("INSERT INTO properties(address_hash,identity_status) VALUES($1,'confirmed') RETURNING property_id","stage1-delivery-"+row["id"])
        await database.execute("INSERT INTO property_listings(property_id,listing_id,link_method) VALUES($1,$2,'manual')",pid,row["id"])
        await database.execute("""INSERT INTO user_listing_notifications(user_id,listing_id,subscription_id,kind,property_id,payload,delivery_status,notified_at)
            VALUES($1,$2,$3,'yield',$4,$5::jsonb,'pending',
              (date_trunc('day',now() AT TIME ZONE 'Asia/Almaty') AT TIME ZONE 'Asia/Almaty') + $6::interval)""",
            user_id,f"yield:{pid}",sid,pid,json.dumps({"listing_id":row["id"]}),timedelta(minutes=-1 if i<5 else 1))
    # Keep the genuine dispatcher SQL and locks, restrict only fixture ownership.
    original=database.fetchrow
    class Scoped:
        def __getattr__(self,name): return getattr(database,name)
        async def fetchrow(self,sql,*args):
            if "WHERE n.delivery_status='pending'" in sql:
                sql=sql.replace("WHERE n.delivery_status='pending'",f"WHERE n.user_id={int(user_id)} AND n.delivery_status='pending'")
            return await original(sql,*args)
    class Pool:
        @asynccontextmanager
        async def acquire(self): yield Scoped()
    monkeypatch.setattr("bot.db.pg.get_pool",lambda:Pool())
    monkeypatch.setattr("bot.db.settings.get_int",lambda key,default:5 if key=="BUYER_ALERT_DAILY_CAP" else default)
    async def stored(row): return json.loads(row["rental_economics"]) if isinstance(row["rental_economics"],str) else row["rental_economics"]
    monkeypatch.setattr("bot.core.rental_economics.rental_economics_for_listing",stored)
    bot=type("MockBot",(),{"send_message":AsyncMock()})()
    assert (await alerts.dispatch_pending(bot,limit=20))["sent"]==5
    assert await database.fetchval("SELECT count(*) FROM user_listing_notifications WHERE user_id=$1 AND delivery_status='pending'",user_id)==5
    assert await database.fetchval("SELECT count(*) FROM user_listing_notifications WHERE user_id=$1 AND attempted_at IS NOT NULL",user_id)==5
    # Advancing reservations out of the current day models tomorrow's budget;
    # deferred entries remain pending and are freshly revalidated, never lost.
    await database.execute("UPDATE user_listing_notifications SET attempted_at=attempted_at-interval '1 day' WHERE user_id=$1",user_id)
    assert (await alerts.dispatch_pending(bot,limit=20))["sent"]==5
    assert bot.send_message.await_count==10
