"""Регрессия для Фазы L3 продуктового трека «Локация» (walkability,
docs/location_product_design.md §3/§4, задача 2026-08-15) — схема
миграции migrations/075_complex_walkability.sql: complex_walkability.
Только схема — osrm_client/snapshot-скрипт покрыты отдельно. Реальная
БД (тот же паттерн, что tests/test_location_market_stats_schema.py)."""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")


@pytest_asyncio.fixture
async def db():
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    yield
    await close_pool()


async def _insert_complex(name):
    from bot.db.pg import fetchval
    return await fetchval("INSERT INTO complexes (name) VALUES ($1) RETURNING id", name)


@pytest.mark.asyncio
async def test_complex_walkability_insert_roundtrip(db):
    from bot.db.pg import execute, fetchrow
    cid = await _insert_complex("__test_cw_roundtrip__")
    try:
        await execute(
            """
            INSERT INTO complex_walkability (
                complex_id, destination_type, walking_distance_m, walking_duration_s,
                haversine_distance_m, ratio, barrier, dest_name, dest_lat, dest_lon,
                complex_lat, complex_lon, engine_version, git_commit
            ) VALUES ($1, 'school', 812.5, 610.0, 205.3, 3.96, TRUE,
                      'Школа №12', 51.13, 71.41, 51.131, 71.413,
                      'osrm-foot-v1@2026-08-15', 'abc1234')
            """,
            cid,
        )
        row = await fetchrow(
            "SELECT * FROM complex_walkability WHERE complex_id=$1", cid)
        assert row["destination_type"] == "school"
        assert row["walking_distance_m"] == pytest.approx(812.5)
        assert row["haversine_distance_m"] == pytest.approx(205.3)
        assert row["ratio"] == pytest.approx(3.96)
        assert row["barrier"] is True
        assert row["dest_name"] == "Школа №12"
        assert row["no_route_reason"] is None
        assert row["engine_version"] == "osrm-foot-v1@2026-08-15"
        assert row["computed_at"] is not None
    finally:
        await execute("DELETE FROM complex_walkability WHERE complex_id=$1", cid)
        await execute("DELETE FROM complexes WHERE id=$1", cid)


@pytest.mark.asyncio
async def test_complex_walkability_no_route_nullable(db):
    """OSRM не построил маршрут (no_snap/новостройка) — walking/ratio NULL,
    но строка пишется: «попытались, вот что знаем» (Unknown ≠ average)."""
    from bot.db.pg import execute, fetchrow
    cid = await _insert_complex("__test_cw_noroute__")
    try:
        await execute(
            """
            INSERT INTO complex_walkability (
                complex_id, destination_type, haversine_distance_m,
                no_route_reason, complex_lat, complex_lon, engine_version
            ) VALUES ($1, 'park', 640.0, 'no_snap', 51.2, 71.5,
                      'osrm-foot-v1@2026-08-15')
            """,
            cid,
        )
        row = await fetchrow(
            "SELECT * FROM complex_walkability WHERE complex_id=$1", cid)
        assert row["walking_distance_m"] is None
        assert row["ratio"] is None
        assert row["barrier"] is None
        assert row["no_route_reason"] == "no_snap"
        assert row["haversine_distance_m"] == pytest.approx(640.0)
    finally:
        await execute("DELETE FROM complex_walkability WHERE complex_id=$1", cid)
        await execute("DELETE FROM complexes WHERE id=$1", cid)


@pytest.mark.asyncio
async def test_complex_walkability_append_only_per_type(db):
    """PK (complex_id, destination_type, computed_at): два снимка одного
    типа сосуществуют; разные типы в один момент — тоже."""
    from bot.db.pg import execute, fetch
    cid = await _insert_complex("__test_cw_append__")
    t0 = datetime.now(timezone.utc) - timedelta(days=30)
    t1 = datetime.now(timezone.utc)
    try:
        for ts, dist in ((t0, 500.0), (t1, 520.0)):
            await execute(
                """
                INSERT INTO complex_walkability (
                    complex_id, destination_type, computed_at,
                    walking_distance_m, haversine_distance_m, engine_version
                ) VALUES ($1, 'transit', $2, $3, 400.0, 'osrm-foot-v1@2026-08-15')
                """,
                cid, ts, dist,
            )
        await execute(
            """
            INSERT INTO complex_walkability (
                complex_id, destination_type, computed_at,
                walking_distance_m, haversine_distance_m, engine_version
            ) VALUES ($1, 'shop', $2, 300.0, 250.0, 'osrm-foot-v1@2026-08-15')
            """,
            cid, t1,
        )
        rows = await fetch(
            "SELECT destination_type, walking_distance_m FROM complex_walkability "
            "WHERE complex_id=$1 ORDER BY computed_at, destination_type", cid)
        assert [(r["destination_type"], round(r["walking_distance_m"])) for r in rows] == [
            ("transit", 500), ("shop", 300), ("transit", 520)]
    finally:
        await execute("DELETE FROM complex_walkability WHERE complex_id=$1", cid)
        await execute("DELETE FROM complexes WHERE id=$1", cid)


@pytest.mark.asyncio
async def test_complex_walkability_cascade_delete_with_complex(db):
    from bot.db.pg import execute, fetch
    cid = await _insert_complex("__test_cw_cascade__")
    await execute(
        """
        INSERT INTO complex_walkability (
            complex_id, destination_type, haversine_distance_m, engine_version
        ) VALUES ($1, 'kindergarten', 350.0, 'osrm-foot-v1@2026-08-15')
        """,
        cid,
    )
    await execute("DELETE FROM complexes WHERE id=$1", cid)
    rows = await fetch("SELECT * FROM complex_walkability WHERE complex_id=$1", cid)
    assert rows == []
