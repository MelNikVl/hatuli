"""Opt-in PostgreSQL checks for pre-applied rental migrations 109/110.

Set HATULI_RENTAL_STAGE0_TEST_DATABASE_URL explicitly to a local hatuli_test
database on port 15439 or 5432. Ambient DATABASE_URL is never used. This module does
not initialize application pools or apply migrations. Each writable test uses
one raw asyncpg connection and an outer transaction that is always rolled back.
The maintenance CLI borrows that connection, so its transactions are genuine
PostgreSQL savepoints; successful writes never escape the test fixture.
"""
from __future__ import annotations

import os
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio


OPT_IN = "HATULI_RENTAL_STAGE0_TEST_DATABASE_URL"


def _validated_test_dsn(value):
    """Reject alternate host/database query options before any connection."""
    try:
        parsed = urlsplit(value)
        valid = (isinstance(value, str) and value == value.strip()
                 and not any(ord(char) < 32 for char in value)
                 and parsed.scheme in ("postgres", "postgresql")
                 and parsed.hostname in ("127.0.0.1", "localhost")
                 and parsed.port in (15439, 5432)
                 and parsed.path == "/hatuli_test"
                 and not parsed.query and not parsed.fragment)
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise ValueError("Rental PG tests require an explicit local hatuli_test destination")
    return value


async def _raw_connect(dsn, **kwargs):
    # Keep this guard next to every real connection, including tool.run().
    return await asyncpg.connect(_validated_test_dsn(dsn), **kwargs)


def _write_env(path, dsn):
    # Keep the explicit DSN literal when dotenv reads the private temporary file.
    escaped = dsn.replace("\\", "\\\\").replace("'", "\\'")
    path.write_text("DATABASE_URL='"+escaped+"'\n", encoding="utf-8")
    os.chmod(path, 0o600)


@pytest_asyncio.fixture
async def stage0_pg(monkeypatch):
    supplied = os.environ.get(OPT_IN)
    if not supplied:
        pytest.skip(f"Set {OPT_IN} explicitly to run isolated PostgreSQL checks")
    try:
        dsn = _validated_test_dsn(supplied)
    except ValueError:
        pytest.fail("Unsafe rental PostgreSQL test destination; no connection attempted", pytrace=False)

    monkeypatch.setattr("dotenv.load_dotenv", lambda *args, **kwargs: False)
    from bot.db import pg
    from scripts import rental_stage0_maintenance as tool

    def deny_application_database(*args, **kwargs):
        raise AssertionError("These tests must not initialize pools or migrations")

    monkeypatch.setattr(pg, "init_pool", deny_application_database)
    monkeypatch.setattr(pg, "get_pool", deny_application_database)
    monkeypatch.setattr(pg, "_apply_migrations", deny_application_database)
    monkeypatch.setattr(asyncpg, "create_pool", deny_application_database)
    try:
        connection = await _raw_connect(dsn, timeout=5, command_timeout=15,
                                        server_settings={"application_name": "hatuli-rental-stage0-pg-test",
                                                         "lock_timeout": "2000", "statement_timeout": "15000"})
    except Exception as exc:
        pytest.fail(f"Opt-in PostgreSQL connection failed ({type(exc).__name__})", pytrace=False)
    outer = connection.transaction(isolation="repeatable_read")
    started = False
    try:
        assert await connection.fetchval("SELECT current_database()") == "hatuli_test"
        assert connection.get_server_version().major >= 15, "Migration 110 requires PostgreSQL 15+"
        await outer.start()
        started = True
        status = await tool.schema_status(connection)
        assert status["ready"], "Apply migrations 109/110 externally before opting in"
        yield SimpleNamespace(connection=connection, dsn=dsn, tool=tool,
                              prefix="~rental_stage0_pg_"+uuid4().hex+"_")
    finally:
        if started:
            await outer.rollback()
        await connection.close()


class _BorrowedConnection:
    """Forward all SQL to real PG; keep its rollback fixture in charge of close."""
    def __init__(self, connection, fail_on_update=None):
        self.connection = connection
        self.fail_on_update = fail_on_update
        self.update_count = 0
        self.close_requested = False

    def __getattr__(self, name):
        return getattr(self.connection, name)

    async def execute(self, sql, *args):
        if sql.startswith("UPDATE rental_listings SET"):
            self.update_count += 1
            if self.update_count == self.fail_on_update:
                # A real server error after a real first UPDATE exercises PG rollback.
                return await self.connection.execute("SELECT 1 / 0")
        return await self.connection.execute(sql, *args)

    async def close(self):
        self.close_requested = True


async def _setting(pg, key, value):
    await pg.connection.execute("INSERT INTO app_settings(key,value) VALUES($1,$2) "
                                "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value", key, value)


async def _seed_rentals(pg, titles=("Квартира в месяц",)):
    await _setting(pg, "HEX_EDGE_M", "50")
    name = "Rental stage0 PG "+pg.prefix
    url = "https://krisha.kz/complex/show/astana/stage0-"+uuid4().hex
    cid = await pg.connection.fetchval("INSERT INTO complexes(name,krisha_url,is_umbrella,is_garbage,is_street) "
                                       "VALUES($1,$2,FALSE,FALSE,FALSE) RETURNING id", name, url)
    ids = []
    for index, title in enumerate(titles):
        identifier = pg.prefix+f"{index:04d}"
        await pg.connection.execute("""
            INSERT INTO rental_listings(id,title,price,area,rooms,city,prop_type,complex_name,complex_url,
                                        lat,lon,rent_period,last_seen)
            VALUES($1,$2,250000,60,2,'astana','apartment',$3,$4,51.12,71.43,'unknown',now())
        """, identifier, title, name, url)
        ids.append(identifier)
    return SimpleNamespace(ids=ids, cid=cid, name=name, url=url)


async def _review_plan(pg, tmp_path, count=1, operation="backfill"):
    plan = await pg.tool.build_plan(pg.connection, operation, limit=count, after_id=pg.prefix)
    assert len(plan["changes"]) == count
    assert all(change["id"].startswith(pg.prefix) for change in plan["changes"])
    assert plan["database_writes"] == 0 and plan["dry_run"] is True
    path = tmp_path/"private"/"reviewed-plan.json"
    pg.tool.private_json(path, plan)
    return pg.tool.load_plan(path), path


async def _apply_reviewed(pg, tmp_path, plan, path, borrowed=None):
    borrowed = borrowed or _BorrowedConnection(pg.connection)
    env = tmp_path/"explicit-test.env"
    _write_env(env, pg.dsn)
    args = SimpleNamespace(operation=plan["operation"], apply=True, plan=path,
                           expected_plan_sha=plan["plan_sha256"], env_file=env,
                           output=tmp_path/"private"/"application-report.json", limit=1, after_id="")

    async def borrow(dsn, **kwargs):
        assert _validated_test_dsn(dsn) == pg.dsn
        assert kwargs["server_settings"]["default_transaction_read_only"] == "off"
        return borrowed

    result = await pg.tool.run(args, connect=borrow)
    assert borrowed.close_requested
    assert pg.tool.load_plan(args.output)["database_writes"] == result["database_writes"]
    return result


def test_opt_in_guard_accepts_only_explicit_local_test_database():
    for host in ("127.0.0.1", "localhost"):
        for port in (15439, 5432):
            value = f"postgresql://test:test@{host}:{port}/hatuli_test"
            assert _validated_test_dsn(value) == value


def test_opt_in_guard_rejects_remote_ambient_and_query_overrides_before_connect():
    for value in (None, "", "postgresql://localhost/hatuli_test",
                  "postgresql://localhost:5432/production", "postgresql://192.168.0.15:5432/hatuli_test",
                  "postgresql://127.0.0.1:8082/hatuli_test", "postgresql://localhost:5432/hatuli_test?host=remote",
                  "postgresql://localhost:5432/hatuli_test?dbname=production",
                  "postgresql://localhost:5432/hatuli_test#fragment", "postgresql://localhost:5432/%68atuli_test",
                  "postgresql://localhost:bad/hatuli_test", " postgresql://localhost:5432/hatuli_test"):
        with pytest.raises(ValueError, match="local hatuli_test"):
            _validated_test_dsn(value)


@pytest.mark.asyncio
async def test_109_legacy_period_remains_unknown_without_amount_inference(stage0_pg):
    pg = stage0_pg
    await pg.connection.execute("INSERT INTO rental_listings(id,price) VALUES($1,300000)", pg.prefix)
    row = await pg.connection.fetchrow("SELECT rent_period,rent_period_conflict,hex_id,hex_edge_m "
                                        "FROM rental_listings WHERE id=$1", pg.prefix)
    assert tuple(row.values()) == ("unknown", False, None, None)


@pytest.mark.asyncio
async def test_109_and_110_reject_unsupported_periods(stage0_pg):
    pg = stage0_pg
    for table in ("rental_listings", "rental_index"):
        with pytest.raises(asyncpg.CheckViolationError):
            async with pg.connection.transaction():
                if table == "rental_listings":
                    await pg.connection.execute("INSERT INTO rental_listings(id,rent_period) VALUES($1,'hourly')", pg.prefix)
                else:
                    await pg.connection.execute("INSERT INTO rental_index(city,rent_period) VALUES($1,'hourly')", pg.prefix)


@pytest.mark.asyncio
async def test_109_rejects_both_half_populated_hex_pairs(stage0_pg):
    pg = stage0_pg
    for hex_id, edge in (("1:-2", None), (None, 50.0)):
        with pytest.raises(asyncpg.CheckViolationError):
            async with pg.connection.transaction():
                await pg.connection.execute("INSERT INTO rental_listings(id,hex_id,hex_edge_m) VALUES($1,$2,$3)",
                                             pg.prefix, hex_id, edge)
    assert await pg.connection.fetchval("SELECT count(*) FROM rental_listings WHERE id=$1", pg.prefix) == 0


@pytest.mark.asyncio
async def test_109_accepts_valid_hex_pair_and_rejects_bad_coordinate_key(stage0_pg):
    pg = stage0_pg
    await pg.connection.execute("INSERT INTO rental_listings(id,hex_id,hex_edge_m) VALUES($1,'1:-2',50)", pg.prefix)
    with pytest.raises(asyncpg.CheckViolationError):
        async with pg.connection.transaction():
            await pg.connection.execute("UPDATE rental_listings SET hex_id='not-a-hex' WHERE id=$1", pg.prefix)
    assert await pg.connection.fetchval("SELECT hex_id FROM rental_listings WHERE id=$1", pg.prefix) == "1:-2"


@pytest.mark.asyncio
async def test_110_same_nullable_scope_has_separate_monthly_daily_unique_keys(stage0_pg):
    pg = stage0_pg
    for period in ("monthly", "daily"):
        await pg.connection.execute("INSERT INTO rental_index(city,rooms,rent_period) VALUES($1,2,$2)", pg.prefix, period)
    for period in ("monthly", "daily"):
        with pytest.raises(asyncpg.UniqueViolationError):
            async with pg.connection.transaction():
                await pg.connection.execute("INSERT INTO rental_index(city,rooms,rent_period) VALUES($1,2,$2)", pg.prefix, period)
    assert await pg.connection.fetchval("SELECT count(*) FROM rental_index WHERE city=$1", pg.prefix) == 2


@pytest.mark.asyncio
async def test_maintenance_audit_uses_real_readonly_connection(stage0_pg, tmp_path):
    pg = stage0_pg
    env = tmp_path/"audit.env"
    _write_env(env, pg.dsn)
    args = SimpleNamespace(operation="audit", apply=False, plan=None, expected_plan_sha=None,
                           env_file=env, output=tmp_path/"private"/"audit.json", limit=1, after_id="")
    connections = []

    async def connect(dsn, **kwargs):
        assert kwargs["server_settings"]["default_transaction_read_only"] == "on"
        connection = await _raw_connect(dsn, **kwargs)
        connections.append(connection)
        try:
            assert await connection.fetchval("SHOW default_transaction_read_only") == "on"
        except BaseException:
            await connection.close()
            raise
        return connection

    result = await pg.tool.run(args, connect=connect)
    assert result["dry_run"] is True and result["database_writes"] == 0
    assert result["changes"] == [] and connections[0].is_closed()
    assert pg.tool.load_plan(args.output)["plan_sha256"] == result["plan_sha256"]
    if os.name == "posix":
        assert args.output.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_maintenance_real_backfill_roundtrip_preserves_other_columns(stage0_pg, tmp_path):
    pg = stage0_pg
    seeded = await _seed_rentals(pg, ("Квартира в месяц", "Квартира за сутки"))
    before = [dict(row) for row in await pg.connection.fetch("SELECT * FROM rental_listings WHERE id=ANY($1::text[]) ORDER BY id", seeded.ids)]
    plan, path = await _review_plan(pg, tmp_path, count=2)
    assert await pg.connection.fetchval("SELECT count(*) FROM rental_listings WHERE id=ANY($1::text[]) AND rent_period='unknown'", seeded.ids) == 2
    report = await _apply_reviewed(pg, tmp_path, plan, path)
    after = [dict(row) for row in await pg.connection.fetch("SELECT * FROM rental_listings WHERE id=ANY($1::text[]) ORDER BY id", seeded.ids)]
    assert report["database_writes"] == 2
    assert [row["rent_period"] for row in after] == ["monthly", "daily"]
    for old, new in zip(before, after):
        assert new["complex_id"] == seeded.cid and new["hex_id"] and new["hex_edge_m"] == 50
        assert not new["rent_period_conflict"]
        allowed = set(pg.tool.WRITABLE["backfill"][2])
        assert {key: value for key, value in old.items() if key not in allowed} == {
            key: value for key, value in new.items() if key not in allowed}


@pytest.mark.asyncio
async def test_maintenance_real_row_change_aborts_whole_batch(stage0_pg, tmp_path):
    pg = stage0_pg
    seeded = await _seed_rentals(pg, ("Квартира в месяц", "Квартира в месяц"))
    plan, path = await _review_plan(pg, tmp_path, count=2)
    await pg.connection.execute("UPDATE rental_listings SET price=260000 WHERE id=$1", seeded.ids[1])
    with pytest.raises(pg.tool.MaintenanceError, match="source row changed"):
        await _apply_reviewed(pg, tmp_path, plan, path)
    assert await pg.connection.fetchval("SELECT count(*) FROM rental_listings WHERE id=ANY($1::text[]) AND rent_period='unknown'", seeded.ids) == 2


@pytest.mark.asyncio
async def test_maintenance_real_resolver_evidence_change_invalidates_plan(stage0_pg, tmp_path):
    pg = stage0_pg
    seeded = await _seed_rentals(pg)
    plan, path = await _review_plan(pg, tmp_path)
    await pg.connection.execute("UPDATE complexes SET krisha_url=NULL WHERE id=$1", seeded.cid)
    with pytest.raises(pg.tool.MaintenanceError, match="evidence changed"):
        await _apply_reviewed(pg, tmp_path, plan, path)
    assert await pg.connection.fetchval("SELECT rent_period FROM rental_listings WHERE id=$1", seeded.ids[0]) == "unknown"


@pytest.mark.asyncio
async def test_maintenance_real_settings_change_invalidates_plan(stage0_pg, tmp_path):
    pg = stage0_pg
    seeded = await _seed_rentals(pg)
    plan, path = await _review_plan(pg, tmp_path)
    await _setting(pg, "HEX_EDGE_M", "100")
    with pytest.raises(pg.tool.MaintenanceError, match="Settings changed"):
        await _apply_reviewed(pg, tmp_path, plan, path)
    assert await pg.connection.fetchval("SELECT rent_period FROM rental_listings WHERE id=$1", seeded.ids[0]) == "unknown"


@pytest.mark.asyncio
async def test_maintenance_real_server_error_rolls_back_first_update(stage0_pg, tmp_path):
    pg = stage0_pg
    seeded = await _seed_rentals(pg, ("Квартира в месяц", "Квартира в месяц"))
    plan, path = await _review_plan(pg, tmp_path, count=2)
    borrowed = _BorrowedConnection(pg.connection, fail_on_update=2)
    with pytest.raises(asyncpg.DivisionByZeroError):
        await _apply_reviewed(pg, tmp_path, plan, path, borrowed)
    assert borrowed.update_count == 2 and borrowed.close_requested
    rows = await pg.connection.fetch("SELECT rent_period,complex_id,hex_id,hex_edge_m FROM rental_listings "
                                      "WHERE id=ANY($1::text[])", seeded.ids)
    assert len(rows) == 2 and all(tuple(row.values()) == ("unknown", None, None, None) for row in rows)


@pytest.mark.asyncio
async def test_maintenance_real_recompute_writes_numeric_types_and_preserves_score(stage0_pg, tmp_path):
    pg = stage0_pg
    seeded = await _seed_rentals(pg)
    for key, value in (("RENTAL_INDEX_POLICY_VERSION", "3"), ("RENT_MIN_SAMPLE_COMPLEX", "5"),
                       ("NET_YIELD_VACANCY", ".95"), ("NET_YIELD_OPEX_SHARE", ".10"), ("NET_YIELD_PURCHASE_COST", "1.02")):
        await _setting(pg, key, value)
    await pg.connection.execute("""
        INSERT INTO rental_index(city,complex_name,complex_id,rooms,rent_period,median_price,avg_price,
                                 p25_price,p75_price,sample_count,price_per_sqm,updated_at,window_days,
                                 active_supply,source_oldest_at,source_newest_at,policy_version)
        VALUES('astana',$1,$2,2,'monthly',280000,280000,250000,310000,5,5000,now(),30,5,
               now()-interval '2 hours',now()-interval '1 hour','3')
    """, seeded.name, seeded.cid)
    identifier = pg.prefix+"0000"
    await pg.connection.execute("""
        INSERT INTO apartment_listings(id,price,area,rooms,city,complex_name,complex_id,complex_resolution,
                                       last_seen,is_active,est_rent,score_total)
        VALUES($1,40000000,60,2,'astana',$2,$3,'url',now(),TRUE,999,73)
    """, identifier, seeded.name, seeded.cid)
    plan, path = await _review_plan(pg, tmp_path, operation="recompute")
    result = await _apply_reviewed(pg, tmp_path, plan, path)
    row = await pg.connection.fetchrow("SELECT price,area,est_rent,yield_pct,net_yield_pct,payback_years,rent_source,score_total "
                                        "FROM apartment_listings WHERE id=$1", identifier)
    assert result["database_writes"] == 1 and result["deal_score_recompute_required"] is True
    assert row["est_rent"] == 300000 and float(row["yield_pct"]) == 9.0
    assert row["net_yield_pct"] == pytest.approx(7.5) and float(row["payback_years"]) == 11.1
    assert row["rent_source"] == "ЖК, n=5"
    assert (row["price"], row["area"], row["score_total"]) == (40000000, 60.0, 73)
