"""Регрессия для Seller Profile (§2.7 docs/liquidity_model_design.md,
задача 2026-08-15) — схема миграции migrations/077_seller_profiles.sql:
seller_profiles. Только схема/UPSERT-контракт — агрегационная логика
seller_profile_snapshot.py покрыта отдельно
(tests/test_seller_profile_snapshot.py), реальная БД (тот же паттерн,
что tests/test_complex_walkability_schema.py)."""
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


async def _cleanup(name: str) -> None:
    from bot.db.pg import execute
    await execute("DELETE FROM seller_profiles WHERE seller_name=$1", name)


@pytest.mark.asyncio
async def test_seller_profiles_insert_roundtrip(db):
    from bot.db.pg import execute, fetchrow
    name = "__test_sp_roundtrip__"
    try:
        await execute(
            """
            INSERT INTO seller_profiles (
                seller_name, seller_type, active_listings_count, total_listings_count,
                relist_count, relist_rate, price_cut_count, price_cut_rate,
                avg_days_to_sell, median_discount_pct,
                is_high_relist_rate, is_motivated_seller, is_ambiguous
            ) VALUES ($1, 'realtor', 5, 12, 4, 0.3333, 6, 0.5, 21.5, 3.2, TRUE, FALSE, FALSE)
            """,
            name,
        )
        row = await fetchrow("SELECT * FROM seller_profiles WHERE seller_name=$1", name)
        assert row["seller_type"] == "realtor"
        assert row["active_listings_count"] == 5
        assert row["total_listings_count"] == 12
        assert row["relist_count"] == 4
        # NUMERIC приходит из asyncpg как Decimal — приводим к float перед
        # approx (pytest.approx не умеет float-Decimal напрямую).
        assert float(row["relist_rate"]) == pytest.approx(0.3333)
        assert row["price_cut_count"] == 6
        assert float(row["price_cut_rate"]) == pytest.approx(0.5)
        assert float(row["avg_days_to_sell"]) == pytest.approx(21.5)
        assert float(row["median_discount_pct"]) == pytest.approx(3.2)
        assert row["is_high_relist_rate"] is True
        assert row["is_motivated_seller"] is False
        assert row["is_ambiguous"] is False
        assert row["computed_at"] is not None
    finally:
        await _cleanup(name)


@pytest.mark.asyncio
async def test_seller_profiles_defaults_on_minimal_insert(db):
    """Только PK + обязательные счётчики — остальные NUMERIC-метрики
    (relist_rate/avg_days_to_sell/median_discount_pct) валидно NULL, если
    исходных данных не было (Unknown ≠ average), bool-флаги дефолтятся в
    FALSE, не NULL (в отличие от outcome_labels — здесь не троичная
    семантика, флаг либо посчитан TRUE, либо нет данных = FALSE)."""
    from bot.db.pg import execute, fetchrow
    name = "__test_sp_minimal__"
    try:
        await execute("INSERT INTO seller_profiles (seller_name) VALUES ($1)", name)
        row = await fetchrow("SELECT * FROM seller_profiles WHERE seller_name=$1", name)
        assert row["active_listings_count"] == 0
        assert row["total_listings_count"] == 0
        assert row["relist_rate"] is None
        assert row["avg_days_to_sell"] is None
        assert row["median_discount_pct"] is None
        assert row["is_high_relist_rate"] is False
        assert row["is_motivated_seller"] is False
        assert row["is_ambiguous"] is False
    finally:
        await _cleanup(name)


@pytest.mark.asyncio
async def test_seller_profiles_upsert_by_name(db):
    """PK = seller_name — повторный снимок обновляет строку на месте
    (текущее состояние, не append-only история), тот же паттерн, что
    outcome_labels (065)."""
    from bot.db.pg import execute, fetchrow
    name = "__test_sp_upsert__"
    upsert_sql = """
        INSERT INTO seller_profiles (seller_name, active_listings_count, total_listings_count)
        VALUES ($1, $2, $3)
        ON CONFLICT (seller_name) DO UPDATE SET
            active_listings_count = EXCLUDED.active_listings_count,
            total_listings_count = EXCLUDED.total_listings_count,
            computed_at = now()
    """
    try:
        await execute(upsert_sql, name, 3, 10)
        first = await fetchrow("SELECT * FROM seller_profiles WHERE seller_name=$1", name)
        await execute(upsert_sql, name, 7, 11)
        second = await fetchrow("SELECT * FROM seller_profiles WHERE seller_name=$1", name)

        assert first["active_listings_count"] == 3
        assert second["active_listings_count"] == 7
        assert second["total_listings_count"] == 11
        # Одна строка на продавца, не история версий.
        count_row = await fetchrow(
            "SELECT count(*) AS n FROM seller_profiles WHERE seller_name=$1", name)
        assert count_row["n"] == 1
        assert second["computed_at"] >= first["computed_at"]
    finally:
        await _cleanup(name)
