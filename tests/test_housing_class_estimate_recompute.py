"""Регрессия для housing_class_estimate_recompute.py — Часть 2, п.11
(задача 2026-08-14, "скоринг волна 2"): пересчёт housing_class_estimate
+ housing_class_estimate_computed_at (не молчаливая заморозка, урок Г3 —
computed_at обязателен, docs/temporal_policy.md)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")


def test_estimate_class_high_price_percentile_is_premium():
    from housing_class_estimate_recompute import estimate_class
    label, score = estimate_class(price_percentile=0.95, ceiling_height=3.2)
    assert label == "премиум"
    assert score > 75


def test_estimate_class_low_price_percentile_is_economy():
    from housing_class_estimate_recompute import estimate_class
    label, score = estimate_class(price_percentile=0.05, ceiling_height=2.6)
    assert label == "эконом"


def test_estimate_class_no_ceiling_data_still_works():
    from housing_class_estimate_recompute import estimate_class
    label, score = estimate_class(price_percentile=0.6, ceiling_height=None)
    assert label in ("комфорт", "бизнес")


def test_estimate_class_high_ceiling_pushes_class_up():
    from housing_class_estimate_recompute import estimate_class
    low_ceiling, score_low = estimate_class(0.5, 2.5)
    high_ceiling, score_high = estimate_class(0.5, 3.3)
    assert score_high > score_low


@pytest_asyncio.fixture
async def db():
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    yield
    await close_pool()


@pytest_asyncio.fixture
async def complex_without_class(db):
    """1 ЖК без housing_class, с avg_price_m2 + 1 объявление с ceiling_height,
    чтобы recompute нашёл и оценил именно его."""
    from bot.db.pg import fetchval, execute
    complex_id = await fetchval("""
        INSERT INTO complexes (name, avg_price_m2) VALUES ('__test_hce_complex__', 700000) RETURNING id
    """)
    await execute("""
        INSERT INTO apartment_listings (id, complex_name, ceiling_height, is_active)
        VALUES ('__test_hce_listing__', '__test_hce_complex__', 3.0, TRUE)
    """)
    try:
        yield complex_id
    finally:
        await execute("DELETE FROM apartment_listings WHERE id = '__test_hce_listing__'")
        await execute("DELETE FROM complexes WHERE id = $1", complex_id)


@pytest.mark.asyncio
async def test_recompute_writes_estimate_and_computed_at(complex_without_class):
    from housing_class_estimate_recompute import run_recompute
    from bot.db.pg import fetch

    await run_recompute(dry=False)
    row = await fetch(
        "SELECT housing_class_estimate, housing_class_estimate_computed_at FROM complexes WHERE id=$1",
        complex_without_class)
    assert row[0]["housing_class_estimate"] is not None
    assert row[0]["housing_class_estimate_computed_at"] is not None


@pytest.mark.asyncio
async def test_recompute_dry_run_does_not_write(complex_without_class):
    from housing_class_estimate_recompute import run_recompute
    from bot.db.pg import fetch

    await run_recompute(dry=True)
    row = await fetch(
        "SELECT housing_class_estimate FROM complexes WHERE id=$1", complex_without_class)
    assert row[0]["housing_class_estimate"] is None


@pytest.mark.asyncio
async def test_recompute_skips_complexes_with_real_housing_class(db):
    from housing_class_estimate_recompute import run_recompute
    from bot.db.pg import fetchval, execute, fetch

    complex_id = await fetchval("""
        INSERT INTO complexes (name, housing_class, avg_price_m2)
        VALUES ('__test_hce_real_class__', 'элит', 900000) RETURNING id
    """)
    try:
        await run_recompute(dry=False)
        row = await fetch(
            "SELECT housing_class_estimate FROM complexes WHERE id=$1", complex_id)
        assert row[0]["housing_class_estimate"] is None  # не трогаем — housing_class уже есть
    finally:
        await execute("DELETE FROM complexes WHERE id=$1", complex_id)
