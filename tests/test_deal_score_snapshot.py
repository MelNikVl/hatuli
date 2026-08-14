"""Регрессия для Фазы A.5, п.2-3 вердикт-стратегии (docs/verdict_strategy.md,
задача 2026-08-14): deal_score_snapshot.py — НЕ пересчитывает формулу,
просто читает текущее apartment_listings.score_total/hex_details и
фиксирует снимок с score_version/git_commit. Реальная БД (тот же паттерн,
что tests/test_effective_score.py)."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

_HEX_DETAILS = {
    "version": 4,
    "components": {
        "price": {"score": 84, "weight": 0.53, "text": "..."},
        "quality": {"score": 75, "weight": 0.27, "text": "..."},
        "market": {"score": 71, "weight": 0.2, "text": "..."},
        "risk": {"score": 70, "weight": 0.0, "text": "..."},
        "location": {"score": 44, "weight": 0.0, "text": "..."},
    },
    "flags": ["⚠ риелтор (+комиссия)", "⚠ класс ЖК не известен"],
}


@pytest_asyncio.fixture
async def db():
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    yield
    await close_pool()


@pytest.mark.asyncio
async def test_snapshot_reads_current_state_without_recomputing(db):
    from bot.db.pg import execute, fetch
    from deal_score_snapshot import run_snapshot

    lid = "__test_dss_basic__"
    await execute(
        """
        INSERT INTO apartment_listings
            (id, price, area, rooms, is_active, score_total, deal_confidence,
             hex_details, bargain_target, bargain_discount_pct, comparables_cnt, bargain_method)
        VALUES ($1, 30000000, 60, 2, TRUE, 79, 80, $2::jsonb, 28000000, 6.5, 12, 'hex+ring+class')
        """,
        lid, json.dumps(_HEX_DETAILS, ensure_ascii=False),
    )
    try:
        await run_snapshot(listing_ids=[lid])
        rows = await fetch(
            "SELECT * FROM deal_score_snapshots WHERE listing_id=$1", lid)
        assert len(rows) == 1
        r = rows[0]
        assert r["score_version"] == 4
        assert r["score_total"] == 79
        assert r["price_score"] == 84
        assert r["quality_score"] == 75
        assert r["market_score"] == 71
        assert r["risk_score"] == 70
        assert json.loads(r["risk_flags"]) == _HEX_DETAILS["flags"]
        assert r["deal_confidence"] == 80
        assert r["data_completeness"] == 80
        assert r["bargain_target_price"] == 28000000
        assert r["bargain_discount_pct"] == pytest.approx(6.5, abs=0.01)
        assert r["bargain_analogs_count"] == 12
        assert r["bargain_method"] == "hex+ring+class"
        assert r["git_commit"]  # непустой хэш
        assert r["inputs_hash"]  # непустой md5
    finally:
        await execute("DELETE FROM deal_score_snapshots WHERE listing_id=$1", lid)
        await execute("DELETE FROM apartment_listings WHERE id=$1", lid)


@pytest.mark.asyncio
async def test_snapshot_skips_archived_listings(db):
    from bot.db.pg import execute, fetch
    from deal_score_snapshot import run_snapshot

    lid = "__test_dss_archived__"
    await execute(
        """
        INSERT INTO apartment_listings (id, price, area, is_active, score_total, hex_details)
        VALUES ($1, 30000000, 60, FALSE, 50, $2::jsonb)
        """,
        lid, json.dumps(_HEX_DETAILS, ensure_ascii=False),
    )
    try:
        await run_snapshot(listing_ids=[lid])
        rows = await fetch("SELECT * FROM deal_score_snapshots WHERE listing_id=$1", lid)
        assert len(rows) == 0
    finally:
        await execute("DELETE FROM deal_score_snapshots WHERE listing_id=$1", lid)
        await execute("DELETE FROM apartment_listings WHERE id=$1", lid)


@pytest.mark.asyncio
async def test_snapshot_skips_listings_without_hex_details(db):
    from bot.db.pg import execute, fetch
    from deal_score_snapshot import run_snapshot

    lid = "__test_dss_no_hex__"
    await execute(
        "INSERT INTO apartment_listings (id, price, area, is_active) VALUES ($1, 30000000, 60, TRUE)",
        lid,
    )
    try:
        await run_snapshot(listing_ids=[lid])
        rows = await fetch("SELECT * FROM deal_score_snapshots WHERE listing_id=$1", lid)
        assert len(rows) == 0
    finally:
        await execute("DELETE FROM deal_score_snapshots WHERE listing_id=$1", lid)
        await execute("DELETE FROM apartment_listings WHERE id=$1", lid)


@pytest.mark.asyncio
async def test_snapshot_is_append_only_not_upsert(db):
    # Два прогона подряд -> ДВЕ строки (не ON CONFLICT-коллапс) — так и
    # задумано (докстринг deal_score_snapshot.py: observed_at/created_at
    # разведены сознательно, идемпотентность не требуется).
    from bot.db.pg import execute, fetch
    from deal_score_snapshot import run_snapshot

    lid = "__test_dss_append__"
    await execute(
        """
        INSERT INTO apartment_listings (id, price, area, is_active, score_total, hex_details)
        VALUES ($1, 30000000, 60, TRUE, 79, $2::jsonb)
        """,
        lid, json.dumps(_HEX_DETAILS, ensure_ascii=False),
    )
    try:
        await run_snapshot(listing_ids=[lid])
        await run_snapshot(listing_ids=[lid])
        rows = await fetch("SELECT * FROM deal_score_snapshots WHERE listing_id=$1", lid)
        assert len(rows) == 2
    finally:
        await execute("DELETE FROM deal_score_snapshots WHERE listing_id=$1", lid)
        await execute("DELETE FROM apartment_listings WHERE id=$1", lid)
