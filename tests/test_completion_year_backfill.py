"""Регрессия для complex_completion_year_backfill.py — Часть 0, задача
2026-08-14 ("быстрые победы"): дозаполнение completion_year/quarter из
homeportal_objects.commissioning_date (DD.MM.YYYY) для complexes.is_newbuild,
идемпотентно (не перезаписывает уже заполненное)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")


def test_parse_commissioning_date_various_months():
    from complex_completion_year_backfill import _parse_commissioning_date
    assert _parse_commissioning_date("14.10.2019") == (2019, 4)
    assert _parse_commissioning_date("01.08.2018") == (2018, 3)
    assert _parse_commissioning_date("22.03.2021") == (2021, 1)
    assert _parse_commissioning_date("15.05.2024") == (2024, 2)


def test_parse_commissioning_date_bad_format_returns_none():
    from complex_completion_year_backfill import _parse_commissioning_date
    assert _parse_commissioning_date("not a date") is None
    assert _parse_commissioning_date("2019-10-14") is None
    assert _parse_commissioning_date("14.10") is None


@pytest_asyncio.fixture
async def db():
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    yield
    await close_pool()


@pytest_asyncio.fixture
async def newbuild_complex_with_homeportal_match(db):
    """1 is_newbuild ЖК без completion_year + 1 homeportal_objects с
    commissioning_date, привязанный через matched_complex_id."""
    from bot.db.pg import fetchval, execute
    complex_id = await fetchval("""
        INSERT INTO complexes (name, is_newbuild) VALUES ('__test_cyb_complex__', TRUE) RETURNING id
    """)
    object_id = await fetchval("SELECT COALESCE(MAX(object_id), 0) + 1 FROM homeportal_objects")
    await execute("""
        INSERT INTO homeportal_objects (object_id, name, commissioning_date, matched_complex_id)
        VALUES ($1, '__test_cyb_hp__', '30.10.2026', $2)
    """, object_id, complex_id)
    try:
        yield complex_id, object_id
    finally:
        await execute("DELETE FROM homeportal_objects WHERE object_id = $1", object_id)
        await execute("DELETE FROM complexes WHERE id = $1", complex_id)


@pytest.mark.asyncio
async def test_backfill_fills_year_and_quarter_from_homeportal(newbuild_complex_with_homeportal_match):
    from complex_completion_year_backfill import run_backfill
    from bot.db.pg import fetch

    complex_id, object_id = newbuild_complex_with_homeportal_match
    result = await run_backfill(dry=False)
    assert result["updated"] >= 1

    row = await fetch("SELECT completion_year, completion_quarter FROM complexes WHERE id=$1", complex_id)
    assert row[0]["completion_year"] == 2026
    assert row[0]["completion_quarter"] == 4  # октябрь -> Q4


@pytest.mark.asyncio
async def test_backfill_dry_run_does_not_write(newbuild_complex_with_homeportal_match):
    from complex_completion_year_backfill import run_backfill
    from bot.db.pg import fetch

    complex_id, object_id = newbuild_complex_with_homeportal_match
    await run_backfill(dry=True)

    row = await fetch("SELECT completion_year FROM complexes WHERE id=$1", complex_id)
    assert row[0]["completion_year"] is None


@pytest.mark.asyncio
async def test_backfill_does_not_overwrite_existing_value(db):
    from complex_completion_year_backfill import run_backfill
    from bot.db.pg import fetchval, execute, fetch

    complex_id = await fetchval("""
        INSERT INTO complexes (name, is_newbuild, completion_year, completion_quarter)
        VALUES ('__test_cyb_existing__', TRUE, 2030, 1) RETURNING id
    """)
    object_id = await fetchval("SELECT COALESCE(MAX(object_id), 0) + 1 FROM homeportal_objects")
    await execute("""
        INSERT INTO homeportal_objects (object_id, name, commissioning_date, matched_complex_id)
        VALUES ($1, '__test_cyb_hp2__', '01.01.2020', $2)
    """, object_id, complex_id)
    try:
        await run_backfill(dry=False)
        row = await fetch("SELECT completion_year FROM complexes WHERE id=$1", complex_id)
        assert row[0]["completion_year"] == 2030  # не тронуто, было уже заполнено
    finally:
        await execute("DELETE FROM homeportal_objects WHERE object_id = $1", object_id)
        await execute("DELETE FROM complexes WHERE id = $1", complex_id)
