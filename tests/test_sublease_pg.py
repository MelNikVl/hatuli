"""Opt-in SQL/API contracts after 109/110/112; no pools or migrations.

Only HATULI_RENTAL_STAGE0_TEST_DATABASE_URL is used, through the existing
loopback/hatuli_test guard. All synthetic writes roll back on one raw
connection. Run serially. Synthetic furniture receipts are test fixtures,
never evidence of a completed real-source furnishing audit.
"""
from datetime import timedelta
import json
import os
from types import SimpleNamespace
from uuid import uuid4

import asyncpg
from fastapi import FastAPI
import httpx
import pytest
import pytest_asyncio

from tests.test_rental_stage0_pg import OPT_IN, _validated_test_dsn, _raw_connect


@pytest_asyncio.fixture
async def sublease_pg(monkeypatch):
    supplied = os.environ.get(OPT_IN)
    if not supplied:
        pytest.skip(f"Set {OPT_IN} explicitly for stage3 PostgreSQL tests")
    try:
        dsn = _validated_test_dsn(supplied)
    except ValueError:
        pytest.fail("Unsafe stage3 PostgreSQL destination; no connection attempted", pytrace=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    from bot.db import pg, settings
    from bot.core import sublease_market as market
    from bot.web import rental_scanner as web
    def deny(*args, **kwargs):
        raise AssertionError("Stage3 PG tests cannot initialize pools or apply migrations")
    for name in ("init_pool", "get_pool", "_apply_migrations"):
        monkeypatch.setattr(pg, name, deny)
    monkeypatch.setattr(asyncpg, "create_pool", deny)
    connection = await _raw_connect(dsn, timeout=5, command_timeout=20,
        server_settings={"application_name": "hatuli-stage3-pg-test", "lock_timeout": "2000", "statement_timeout": "20000"})
    transaction = connection.transaction(isolation="repeatable_read")
    started = False
    try:
        assert await connection.fetchval("SELECT current_database()") == "hatuli_test"
        assert connection.get_server_version().major >= 15
        await transaction.start()
        started = True
        assert await connection.fetchval("SELECT to_regclass('rental_observations')") is not None, "Apply 112 externally"
        assert await connection.fetchval("SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name='rental_index' AND column_name='furnishing_version')"), "Apply 112 externally"
        for name in ("fetch", "fetchrow", "fetchval", "execute"):
            monkeypatch.setattr(pg, name, getattr(connection, name))
        monkeypatch.setattr(market, "fetch", connection.fetch)
        config = dict(settings.DEFAULTS)
        config.update(HEX_EDGE_M="50", RENT_MIN_SAMPLE_COMPLEX="5", RENT_MIN_SAMPLE_AREA="10",
                      RENT_MIN_SAMPLE_HEX="5", RENTAL_FURNISHING_INDEX_ENABLED="0")
        monkeypatch.setattr(settings, "get", lambda key, default=None: config.get(key, default))
        monkeypatch.setattr(web, "_settings", lambda: dict(config))
        await connection.execute("INSERT INTO app_settings(key,value) VALUES('RENTAL_INDEX_POLICY_VERSION','3') "
                                 "ON CONFLICT(key) DO UPDATE SET value='3'")
        prefix = "~sublease_pg_" + uuid4().hex
        district = prefix + " district"
        url = "https://krisha.kz/complex/show/astana/" + uuid4().hex
        name = prefix + " canonical"
        cid = await connection.fetchval("INSERT INTO complexes(name,district,krisha_url,is_umbrella,is_garbage,is_street) "
            "VALUES($1,$2,$3,FALSE,FALSE,FALSE) RETURNING id", name, district, url)
        now = await connection.fetchval("SELECT now()-interval '1 second'")
        state = SimpleNamespace(connection=connection, config=config, market=market, web=web,
            now=now, cid=cid, name=name, district=district, url=url, prefix=prefix,
            next_id=10**13 + int(uuid4().hex[:10], 16))
        yield state
    finally:
        try:
            if started:
                await transaction.rollback()
        finally:
            await connection.close()


async def seed_index(state, period, *, district_scope=False, **changes):
    row = {"city": "astana", "district": state.district if district_scope else None,
        "complex_id": None if district_scope else state.cid, "complex_name": None if district_scope else state.name,
        "rooms": 2, "prop_type": "apartment", "rent_period": period, "furnishing": None,
        "median_price": 20000 if period == "daily" else 300000,
        "p25_price": 15000 if period == "daily" else 250000,
        "p75_price": 25000 if period == "daily" else 350000,
        "sample_count": 10 if district_scope else 5, "active_supply": 5,
        "window_days": 30, "policy_version": "daily_v1" if period == "daily" else "3",
        "source_oldest_at": state.now-timedelta(hours=1), "source_newest_at": state.now,
        "updated_at": state.now, "furnishing_version": None, "median_dom_days": None,
        "gone_30d": None, "months_of_supply": None, **changes}
    keys = tuple(row)
    sql = ("INSERT INTO rental_index("+",".join(keys)+") VALUES("+",".join(f"${n}" for n in range(1, len(keys)+1))+") "
           "ON CONFLICT ON CONSTRAINT rental_index_key_v2 DO UPDATE SET "+
           ",".join(key+"=EXCLUDED."+key for key in keys)+" RETURNING id")
    return await state.connection.fetchval(sql, *row.values())


async def seed_rental(state, **changes):
    identifier = str(state.next_id)
    state.next_id += 1
    from bot.core.hexgrid import hex_id
    row = {"id": identifier, "title": "Квартира в месяц", "price": 200000, "area": 55,
        "rooms": 2, "city": "astana", "district": state.district, "prop_type": "apartment",
        "complex_id": state.cid, "complex_name": state.name, "complex_url": state.url,
        "address": "Туран, 12 телефон +77771234567 owner@example.com",
        "lat": 51.13, "lon": 71.43, "hex_id": hex_id(51.13, 71.43, 50), "hex_edge_m": 50.0,
        "rent_period": "monthly", "rent_period_conflict": False, "is_active": True,
        "is_duplicate": False, "dup_needs_review": False, "duplicate_of": None,
        "found_at": state.now-timedelta(days=10), "last_seen": state.now, "archived_at": None,
        "furnishing": "unknown", "furnishing_version": None, **changes}
    keys = tuple(row)
    await state.connection.execute("INSERT INTO rental_listings("+",".join(keys)+") VALUES("+
        ",".join(f"${n}" for n in range(1,len(keys)+1))+")", *row.values())
    return identifier


def scenario(state, **changes):
    return {"rooms": 2, "complex_id": state.cid, "occupancy": .6, "avg_stay_nights": 2.5,
        "cleaning_per_turn": 3000, "consumables_per_night": 500, "utilities_month": 20000,
        "platform_fee_pct": .1, "tax_pct": .03, "furnish_capex": 600000,
        "capex_months": 24, "deposit_months": 1, **changes}


@pytest.mark.asyncio
async def test_stage3_actual_sql_complex_alias_and_sparse_fallback_no_city(sublease_pg):
    state = sublease_pg
    await seed_index(state, "monthly")
    daily = await seed_index(state, "daily")
    district = await seed_index(state, "daily", district_scope=True)
    alias = await state.connection.fetchval("INSERT INTO complexes(name,canonical_id) VALUES($1,$2) RETURNING id",
                                           state.prefix+" alias", state.cid)
    result = await state.market.read_segment(complex_id=alias, rooms=2, settings=state.config, now=state.now)
    assert result["daily"]["level"] == "complex" and result["daily"]["n"] == 5
    await state.connection.execute("UPDATE rental_index SET sample_count=4 WHERE id=$1", daily)
    result = await state.market.read_segment(complex_id=alias, rooms=2, settings=state.config, now=state.now)
    assert result["daily"]["level"] == "district" and result["daily"]["n"] == 10
    await seed_index(state, "daily", complex_id=None, complex_name=None, sample_count=100)
    await state.connection.execute("UPDATE rental_index SET source_newest_at=$2 WHERE id=$1",
                                   district, state.now-timedelta(days=4))
    result = await state.market.read_segment(complex_id=alias, rooms=2, settings=state.config, now=state.now)
    assert result["daily"] is None and result["monthly"] is not None


@pytest.mark.asyncio
async def test_stage3_actual_candidate_sql_cleans_units_identity_and_source(sublease_pg):
    state = sublease_pg
    for period in ("monthly", "daily"):
        await seed_index(state, period)
        await seed_index(state, period, district_scope=True)
    good = await seed_rental(state)
    ordinary = await seed_rental(state, complex_id=None, complex_name=None, complex_url=None)
    for change in ({"rent_period": "daily"}, {"rent_period": "unknown"}, {"rent_period_conflict": True},
        {"is_duplicate": True}, {"dup_needs_review": True}, {"duplicate_of": good},
        {"is_active": False}, {"last_seen": state.now-timedelta(days=4)},
        {"title": "Квартира посуточно"}, {"complex_url": "https://krisha.kz/complex/show/astana/wrong"}):
        await seed_rental(state, **change)
    garbage = await state.connection.fetchval("INSERT INTO complexes(name,is_garbage) VALUES($1,TRUE) RETURNING id", state.prefix+" junk")
    await seed_rental(state, complex_id=garbage)
    rows, truncated = await state.market.read_candidates({"rooms": 2, "district": state.district}, state.config, now=state.now)
    assert not truncated and {row["id"] for row in rows} == {good, ordinary}
    assert json.loads(rows[0]["daily_index"])["policy_version"] == "daily_v1"


@pytest.mark.asyncio
async def test_stage3_actual_90_day_observations_and_monthly_freshness(sublease_pg):
    state = sublease_pg
    monthly = await seed_index(state, "monthly")
    daily = await seed_index(state, "daily", window_days=90, source_oldest_at=state.now-timedelta(days=80))
    result = await state.market.read_segment(complex_id=state.cid, rooms=2, settings=state.config, now=state.now)
    assert result["daily"]["window_days"] == 90
    await state.connection.execute("UPDATE rental_index SET source_oldest_at=$2 WHERE id=$1", monthly, state.now-timedelta(days=4))
    result = await state.market.read_segment(complex_id=state.cid, rooms=2, settings=state.config, now=state.now)
    assert result["monthly"] is None
    await state.connection.execute("UPDATE rental_index SET active_supply=0 WHERE id=$1", daily)
    result = await state.market.read_segment(complex_id=state.cid, rooms=2, settings=state.config, now=state.now)
    assert result["daily"] is None


@pytest.mark.asyncio
async def test_stage3_actual_api_access_address_privacy_and_calc_sources(sublease_pg, monkeypatch):
    state = sublease_pg
    for period in ("monthly", "daily"):
        await seed_index(state, period)
    identifier = await seed_rental(state)
    tier = {"value": "public"}
    async def current_tier(request):
        return tier["value"]
    monkeypatch.setattr(state.web, "get_user_tier", current_tier)
    app = FastAPI()
    app.include_router(state.web.make_rental_scanner_router(None))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/rental/scanner", params=scenario(state))
        assert response.status_code == 403
        segment = await client.get("/api/rental/segment", params={"rooms": 2, "complex_id": state.cid})
        assert segment.status_code == 200 and "address" not in segment.text
        tier["value"] = "subscriber"
        response = await client.get("/api/rental/scanner", params=scenario(state))
        assert response.status_code == 200
        item = response.json()["items"][0]
        assert item["address"] == "Туран, 12" and item["economics"]["index_source"]["rent_period"] == "daily"
        exported = await client.get("/api/rental/scanner.csv", params=scenario(state))
        assert exported.status_code == 200 and '"Туран, 12"' in exported.text
        for secret in ("owner@example.com", "+77771234567"):
            assert secret not in response.text and secret not in exported.text
        tier["value"] = "public"
        calc = await client.post("/api/rental/calc", json=scenario(state, listing_id=identifier))
        assert calc.status_code == 200 and calc.json()["params_used"]["tax_pct"] == .03
        assert calc.json()["index_source"]["policy_version"] == "daily_v1"
        assert "address" not in calc.json() and "Туран" not in calc.text


@pytest.mark.asyncio
async def test_stage3_actual_furnishing_version_filters_before_district_fallback(sublease_pg):
    from bot.core.rental_furnishing import VERSION, rules_sha256, version_tag
    state = sublease_pg
    receipt = {"schema_version": 1, "classifier_version": VERSION, "eligible_for_operator_enable": True,
        "errors": [], "dataset_kind": "manual_real_source", "unique_reviewed": 50,
        "known_labels_min_support": 5, "accuracy": 1, "dataset_sha256": "a"*64,
        "rules_sha256": rules_sha256("{}"), "reviewed_at": state.now.isoformat()}
    state.config.update(RENTAL_FURNISHING_INDEX_ENABLED="1", RENTAL_FURNISHING_RULES_JSON="{}",
                        RENTAL_FURNISHING_VALIDATION_RECEIPT=json.dumps(receipt))
    version = version_tag("{}")
    for period in ("monthly", "daily"):
        await seed_index(state, period)
    await seed_index(state, "monthly", furnishing="full", policy_version="furnished_monthly_v1", furnishing_version="old_rules")
    await seed_index(state, "monthly", district_scope=True, furnishing="full",
                     policy_version="furnished_monthly_v1", furnishing_version=version)
    await seed_rental(state, furnishing="full", furnishing_version=version)
    rows, _ = await state.market.read_candidates({"rooms": 2, "complex_id": state.cid}, state.config, now=state.now)
    assert len(rows) == 1
    target = json.loads(rows[0]["furnished_monthly_index"])
    assert target["complex_id"] is None and target["furnishing_version"] == version
    economics = state.web._economics(rows[0], scenario(state, strategy="mid_term", vacancy_fraction=.1,
                                                     turnovers_per_year=2), state.config)
    assert economics is not None and economics["index_source"]["level"] == "district"
    assert economics["params_used"]["furnish_capex"] == 0
    assert economics["params_used"]["vacancy_fraction"] == .1


@pytest.mark.asyncio
async def test_stage3_actual_overflow_never_returns_partial_rank_or_csv(sublease_pg, monkeypatch):
    state = sublease_pg
    for period in ("monthly", "daily"):
        await seed_index(state, period)
    await state.connection.executemany("""INSERT INTO rental_listings
      (id,title,price,rooms,city,district,prop_type,complex_id,complex_name,complex_url,rent_period,last_seen)
      VALUES($1,'Квартира в месяц',100000,2,'astana',$2,'apartment',$3,$4,$5,'monthly',$6)""",
      [(str(state.next_id+i),state.district,state.cid,state.name,state.url,state.now) for i in range(501)])
    better = SimpleNamespace(**vars(state))
    better.name = state.prefix+" higher-nightly"
    better.url = "https://krisha.kz/complex/show/astana/"+uuid4().hex
    better.next_id += 10000
    better.cid = await state.connection.fetchval("INSERT INTO complexes(name,district,krisha_url) VALUES($1,$2,$3) RETURNING id",
                                                better.name,better.district,better.url)
    await seed_index(better,"monthly")
    await seed_index(better,"daily",median_price=50000,p25_price=40000,p75_price=60000)
    best_id = await seed_rental(better,price=250000)
    async def paid(request):
        return "subscriber"
    monkeypatch.setattr(state.web,"get_user_tier",paid)
    app=FastAPI()
    app.include_router(state.web.make_rental_scanner_router(None))
    query=scenario(state,district=state.district)
    query.pop("complex_id")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        for path in ("/api/rental/scanner","/api/rental/scanner.csv"):
            response=await client.get(path,params=query)
            assert response.status_code==422 and response.json()["error"]=="narrower_filters_required"
            assert "items" not in response.json() and "source_url" not in response.text
        response=await client.get("/api/rental/scanner",params=query|{"complex_id":better.cid})
        assert response.status_code==200 and response.json()["items"][0]["id"]==best_id


@pytest.mark.asyncio
async def test_stage3_actual_hex_reader_signature_and_provenance(sublease_pg):
    state=sublease_pg
    cell=str(100000+int(uuid4().hex[:5],16)%800000)+":43210"
    for period,policy,price in (("daily","daily_v1",20000),("monthly","hex_monthly_v1",300000)):
        await state.connection.execute("""INSERT INTO rental_hex_index
          (city,hex_id,hex_edge_m,rooms,rent_period,median_price,p25_price,p75_price,sample_count,
           active_supply,window_days,source_oldest_at,source_newest_at,updated_at,policy_version)
          VALUES('astana',$1,50,2,$2,$3,$3,$3,5,5,30,$4,$4,$4,$5)""",cell,period,price,state.now,policy)
    result=await state.market.read_hex_segment(cell,2,state.config)
    assert result["daily"]["level"]=="hex" and result["daily"]["n"]==5
    assert result["monthly"]["policy_version"]=="hex_monthly_v1"
    assert result["daily"]["hex_id"]==cell and result["daily"]["hex_edge_m"]==50
    different=await state.market.read_hex_segment(cell,2,state.config|{"HEX_EDGE_M":"75"})
    assert different["daily"] is None and different["monthly"] is None
