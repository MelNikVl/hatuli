"""Регрессия для housing_class_pre2000_backfill.py (задача 2026-08-14,
read-only-сессия п.4, docs/liquidity_model_design.md §11) — эвристика
year_built<2000 AND housing_class IS NULL -> 'эконом', с явной пометкой
источника (housing_class_source='pre2000_heuristic', migrations/073) —
не молчаливая заморозка, computed_at обязателен (temporal_policy.md,
тот же принцип, что уже применён в housing_class_estimate_recompute.py)."""
import os
import sys

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


async def _insert_complex(name, year_built=None, housing_class=None, is_garbage=False):
    from bot.db.pg import fetchval
    return await fetchval(
        "INSERT INTO complexes (name, year_built, housing_class, is_garbage) "
        "VALUES ($1, $2, $3, $4) RETURNING id",
        name, year_built, housing_class, is_garbage)


async def _cleanup(*ids):
    from bot.db.pg import execute
    await execute("DELETE FROM complexes WHERE id = ANY($1::int[])", list(ids))


@pytest.mark.asyncio
async def test_backfill_writes_class_and_source_for_pre2000_without_class(db):
    from housing_class_pre2000_backfill import run_backfill
    from bot.db.pg import fetchrow

    cid = await _insert_complex("__test_hcpb_pre2000__", year_built=1985, housing_class=None)
    try:
        await run_backfill(dry=False)
        row = await fetchrow(
            "SELECT housing_class, housing_class_source, housing_class_source_computed_at "
            "FROM complexes WHERE id=$1", cid)
        assert row["housing_class"] == "эконом"
        assert row["housing_class_source"] == "pre2000_heuristic"
        assert row["housing_class_source_computed_at"] is not None
    finally:
        await _cleanup(cid)


@pytest.mark.asyncio
async def test_backfill_skips_post2000(db):
    from housing_class_pre2000_backfill import run_backfill
    from bot.db.pg import fetchrow

    cid = await _insert_complex("__test_hcpb_post2000__", year_built=2015, housing_class=None)
    try:
        await run_backfill(dry=False)
        row = await fetchrow(
            "SELECT housing_class, housing_class_source FROM complexes WHERE id=$1", cid)
        assert row["housing_class"] is None
        assert row["housing_class_source"] is None
    finally:
        await _cleanup(cid)


@pytest.mark.asyncio
async def test_backfill_skips_unknown_year_built(db):
    """Unknown != average — без year_built эвристика не применяется
    вовсе, не гадаем на пустом месте."""
    from housing_class_pre2000_backfill import run_backfill
    from bot.db.pg import fetchrow

    cid = await _insert_complex("__test_hcpb_no_year__", year_built=None, housing_class=None)
    try:
        await run_backfill(dry=False)
        row = await fetchrow("SELECT housing_class FROM complexes WHERE id=$1", cid)
        assert row["housing_class"] is None
    finally:
        await _cleanup(cid)


@pytest.mark.asyncio
async def test_backfill_does_not_overwrite_existing_housing_class(db):
    """Ручная метка приоритетнее — тот же принцип, что уже действует в
    housing_class_model_recompute.py/housing_class_estimate_recompute.py."""
    from housing_class_pre2000_backfill import run_backfill
    from bot.db.pg import fetchrow

    cid = await _insert_complex("__test_hcpb_has_class__", year_built=1990, housing_class="бизнес")
    try:
        await run_backfill(dry=False)
        row = await fetchrow(
            "SELECT housing_class, housing_class_source FROM complexes WHERE id=$1", cid)
        assert row["housing_class"] == "бизнес"
        assert row["housing_class_source"] is None  # не тронуто эвристикой
    finally:
        await _cleanup(cid)


@pytest.mark.asyncio
async def test_backfill_skips_garbage_complexes(db):
    from housing_class_pre2000_backfill import run_backfill
    from bot.db.pg import fetchrow

    cid = await _insert_complex("__test_hcpb_garbage__", year_built=1980, housing_class=None, is_garbage=True)
    try:
        await run_backfill(dry=False)
        row = await fetchrow("SELECT housing_class FROM complexes WHERE id=$1", cid)
        assert row["housing_class"] is None
    finally:
        await _cleanup(cid)


@pytest.mark.asyncio
async def test_backfill_dry_run_does_not_write(db):
    from housing_class_pre2000_backfill import run_backfill
    from bot.db.pg import fetchrow

    cid = await _insert_complex("__test_hcpb_dry__", year_built=1975, housing_class=None)
    try:
        result = await run_backfill(dry=True)
        assert result["dry_run"] is True
        assert result["targets"] >= 1  # хотя бы наша тестовая запись найдена
        row = await fetchrow(
            "SELECT housing_class, housing_class_source FROM complexes WHERE id=$1", cid)
        assert row["housing_class"] is None
        assert row["housing_class_source"] is None
    finally:
        await _cleanup(cid)


@pytest.mark.asyncio
async def test_backfill_year_exactly_2000_not_touched(db):
    """Строго < 2000, не <= — граница включает 2000-й год как "новый"."""
    from housing_class_pre2000_backfill import run_backfill
    from bot.db.pg import fetchrow

    cid = await _insert_complex("__test_hcpb_boundary__", year_built=2000, housing_class=None)
    try:
        await run_backfill(dry=False)
        row = await fetchrow("SELECT housing_class FROM complexes WHERE id=$1", cid)
        assert row["housing_class"] is None
    finally:
        await _cleanup(cid)
