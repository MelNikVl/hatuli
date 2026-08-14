"""Регрессия для Фазы L1 продуктового трека «Локация» (docs/location_
product_design.md §7, задача 2026-08-14), коммит 1 — схема миграции
migrations/072_location_market_stats.sql: complex_location_scores,
complex_stats_history (расширение), hex_market_stats. Только схема —
writer-скрипты (complex_location_score_snapshot.py и т.д.) появляются в
следующих коммитах L1, это не их регресс-тесты. Реальная БД (тот же
паттерн, что tests/test_deal_score_snapshot.py)."""
import os
import sys
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import json
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
async def test_complex_location_scores_insert_roundtrip(db):
    from bot.db.pg import execute, fetchrow
    cid = await _insert_complex("__test_cls_roundtrip__")
    try:
        breakdown = {"transport": {"transit_stops": {"adj": 2}}, "informational": {"bank": {"adj": 0}}}
        await execute(
            """
            INSERT INTO complex_location_scores
                (complex_id, score, confidence, transport_score, infra_score,
                 noise_score, green_score, risk_score, lat, lon, breakdown,
                 score_version, git_commit)
            VALUES ($1, 62, 90, 5, 3, -1, 2, -2, 51.15, 71.45, $2::jsonb, 'loc_v1', 'abc1234')
            """,
            cid, json.dumps(breakdown, ensure_ascii=False),
        )
        row = await fetchrow("SELECT * FROM complex_location_scores WHERE complex_id=$1", cid)
        assert row["score"] == 62
        assert row["confidence"] == 90
        assert row["transport_score"] == 5
        assert row["risk_score"] == -2
        assert round(row["lat"], 2) == 51.15
        assert row["score_version"] == "loc_v1"
        assert row["git_commit"] == "abc1234"
        payload = row["breakdown"]
        payload = json.loads(payload) if isinstance(payload, str) else payload
        assert payload["informational"]["bank"]["adj"] == 0
        assert row["computed_at"] is not None
    finally:
        await execute("DELETE FROM complex_location_scores WHERE complex_id=$1", cid)
        await execute("DELETE FROM complexes WHERE id=$1", cid)


@pytest.mark.asyncio
async def test_complex_location_scores_append_only_two_snapshots(db):
    """PRIMARY KEY (complex_id, computed_at) — две строки одного complex_id
    с разным computed_at сосуществуют (append-only, не overwrite)."""
    from bot.db.pg import execute, fetch
    cid = await _insert_complex("__test_cls_append__")
    t0 = datetime.now(timezone.utc) - timedelta(days=30)
    t1 = datetime.now(timezone.utc)
    try:
        for ts, score in ((t0, 40), (t1, 55)):
            await execute(
                """
                INSERT INTO complex_location_scores
                    (complex_id, computed_at, score, confidence, breakdown, score_version)
                VALUES ($1, $2, $3, 80, '{}'::jsonb, 'loc_v1')
                """,
                cid, ts, score,
            )
        rows = await fetch(
            "SELECT score FROM complex_location_scores WHERE complex_id=$1 ORDER BY computed_at", cid)
        assert [r["score"] for r in rows] == [40, 55]
    finally:
        await execute("DELETE FROM complex_location_scores WHERE complex_id=$1", cid)
        await execute("DELETE FROM complexes WHERE id=$1", cid)


@pytest.mark.asyncio
async def test_complex_location_scores_cascade_delete_with_complex(db):
    from bot.db.pg import execute, fetch
    cid = await _insert_complex("__test_cls_cascade__")
    await execute(
        """
        INSERT INTO complex_location_scores (complex_id, score, confidence, breakdown, score_version)
        VALUES ($1, 10, 20, '{}'::jsonb, 'loc_v1')
        """,
        cid,
    )
    await execute("DELETE FROM complexes WHERE id=$1", cid)
    rows = await fetch("SELECT * FROM complex_location_scores WHERE complex_id=$1", cid)
    assert rows == []


@pytest.mark.asyncio
async def test_complex_stats_history_new_columns_nullable_and_writable(db):
    from bot.db.pg import execute, fetchrow
    cid = await _insert_complex("__test_csh_new_cols__")
    try:
        # Без новых колонок — не ломается (writer из коммита 3 ещё не трогает их)
        await execute(
            "INSERT INTO complex_stats_history (complex_id, date, avg_price_m2, listings_count) "
            "VALUES ($1, CURRENT_DATE, 500000, 5)",
            cid,
        )
        row = await fetchrow(
            "SELECT avg_dom_days, price_drop_share_30d, price_drop_share_60d "
            "FROM complex_stats_history WHERE complex_id=$1", cid)
        assert row["avg_dom_days"] is None
        assert row["price_drop_share_30d"] is None

        # С новыми колонками — пишутся штатно
        await execute(
            "UPDATE complex_stats_history SET avg_dom_days=$2, price_drop_share_30d=$3, "
            "price_drop_share_60d=$4 WHERE complex_id=$1",
            cid, 14.5, 0.2, 0.35,
        )
        row = await fetchrow(
            "SELECT avg_dom_days, price_drop_share_30d, price_drop_share_60d "
            "FROM complex_stats_history WHERE complex_id=$1", cid)
        assert row["avg_dom_days"] == pytest.approx(14.5)
        assert row["price_drop_share_30d"] == pytest.approx(0.2)
        assert row["price_drop_share_60d"] == pytest.approx(0.35)
    finally:
        await execute("DELETE FROM complex_stats_history WHERE complex_id=$1", cid)
        await execute("DELETE FROM complexes WHERE id=$1", cid)


@pytest.mark.asyncio
async def test_hex_market_stats_insert_roundtrip(db):
    from bot.db.pg import execute, fetchrow
    hid = "__test_hex_1:2__"
    try:
        await execute(
            """
            INSERT INTO hex_market_stats (hex_id, date, edge_m, listings_count, avg_price_m2)
            VALUES ($1, CURRENT_DATE, 50, 7, 480000)
            """,
            hid,
        )
        row = await fetchrow("SELECT * FROM hex_market_stats WHERE hex_id=$1", hid)
        assert row["listings_count"] == 7
        assert row["edge_m"] == pytest.approx(50.0)
        assert row["avg_price_m2"] == 480000
        assert row["computed_at"] is not None
    finally:
        await execute("DELETE FROM hex_market_stats WHERE hex_id=$1", hid)


@pytest.mark.asyncio
async def test_hex_market_stats_unique_per_hex_and_date(db):
    """PRIMARY KEY (hex_id, date) — повторная вставка того же дня должна
    конфликтовать (writer коммита 4 будет ON CONFLICT DO UPDATE, но сама
    схема обязана держать эту уникальность)."""
    from bot.db.pg import execute
    hid = "__test_hex_unique__"
    await execute(
        "INSERT INTO hex_market_stats (hex_id, date, edge_m, listings_count) VALUES ($1, CURRENT_DATE, 50, 1)",
        hid,
    )
    try:
        with pytest.raises(Exception):
            await execute(
                "INSERT INTO hex_market_stats (hex_id, date, edge_m, listings_count) VALUES ($1, CURRENT_DATE, 50, 2)",
                hid,
            )
    finally:
        await execute("DELETE FROM hex_market_stats WHERE hex_id=$1", hid)
