"""Регрессия для задачи 2026-08-15 ("Реестр КЖК") — схема миграции
migrations/074_kzk_registry.sql: kzk_registry + developers.bin. Только
схема — writer (kzk_registry_collect.py) и matching
(match_kzk_to_complexes()) появляются в следующих коммитах, это не их
регресс-тесты. Реальная БД (тот же паттерн, что tests/test_location_
market_stats_schema.py)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import json
from datetime import date

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


@pytest.mark.asyncio
async def test_kzk_registry_insert_roundtrip(db):
    from bot.db.pg import execute, fetchrow
    await execute("""
        INSERT INTO kzk_registry
            (bin, developer_legal, developer_brand, cities, objects_count, zhk_count,
             by_city, warranty_scheme, is_blacklisted, in_registry, zhk_names, phone,
             source_snapshot_date)
        VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7::jsonb, $8, $9, $10, $11::jsonb, $12, $13::date)
    """,
        "__test_bin_1__", 'ТОО "Тест Девелопмент"', "TestDev",
        json.dumps(["Астана", "Алматы"]), 12, 5,
        json.dumps([["Астана", 8], ["Алматы", 4]]), "Участие БВУ", False, True,
        json.dumps(['ЖК "Тест"']), "+7(700)000-00-00", date(2026, 7, 29),
    )
    try:
        row = await fetchrow("SELECT * FROM kzk_registry WHERE bin=$1", "__test_bin_1__")
        assert row is not None
        assert row["developer_brand"] == "TestDev"
        assert row["warranty_scheme"] == "Участие БВУ"
        assert row["is_blacklisted"] is False
        assert row["in_registry"] is True
        cities = row["cities"]
        cities = json.loads(cities) if isinstance(cities, str) else cities
        assert cities == ["Астана", "Алматы"]
        assert row["developer_id"] is None
        assert row["fetched_at"] is not None
    finally:
        await execute("DELETE FROM kzk_registry WHERE bin=$1", "__test_bin_1__")


@pytest.mark.asyncio
async def test_kzk_registry_unique_bin(db):
    from bot.db.pg import execute
    await execute(
        "INSERT INTO kzk_registry (bin, developer_legal, in_registry) VALUES ($1, $2, TRUE)",
        "__test_bin_unique__", "ТОО Раз")
    try:
        with pytest.raises(Exception):
            await execute(
                "INSERT INTO kzk_registry (bin, developer_legal, in_registry) VALUES ($1, $2, TRUE)",
                "__test_bin_unique__", "ТОО Два")
    finally:
        await execute("DELETE FROM kzk_registry WHERE bin=$1", "__test_bin_unique__")


@pytest.mark.asyncio
async def test_kzk_registry_developer_id_fk_set_null_on_delete(db):
    """ON DELETE SET NULL — если развязанный developers удалён, matching
    не должен ронять всю строку kzk_registry (внешняя запись важнее
    временной связи с нашей внутренней таблицей)."""
    from bot.db.pg import execute, fetchval, fetchrow

    dev_id = await fetchval(
        "INSERT INTO developers (name) VALUES ('__test_dev_for_kzk__') RETURNING id")
    await execute(
        "INSERT INTO kzk_registry (bin, developer_legal, in_registry, developer_id, developer_match_method) "
        "VALUES ($1, $2, TRUE, $3, 'bin')",
        "__test_bin_fk__", "ТОО ФК-тест", dev_id)
    try:
        await execute("DELETE FROM developers WHERE id=$1", dev_id)
        row = await fetchrow("SELECT developer_id FROM kzk_registry WHERE bin=$1", "__test_bin_fk__")
        assert row["developer_id"] is None
    finally:
        await execute("DELETE FROM kzk_registry WHERE bin=$1", "__test_bin_fk__")


@pytest.mark.asyncio
async def test_kzk_registry_blacklisted_and_in_registry_independent(db):
    """Пограничный случай с источника: flagged=true И in_reg=true разом
    (3 из 313 в разведке) — обе колонки хранятся раздельно, не схлопнуты."""
    from bot.db.pg import execute, fetchrow
    await execute(
        "INSERT INTO kzk_registry (bin, developer_legal, in_registry, is_blacklisted) "
        "VALUES ($1, $2, TRUE, TRUE)",
        "__test_bin_edge__", "ТОО Погранично")
    try:
        row = await fetchrow("SELECT is_blacklisted, in_registry FROM kzk_registry WHERE bin=$1", "__test_bin_edge__")
        assert row["is_blacklisted"] is True
        assert row["in_registry"] is True
    finally:
        await execute("DELETE FROM kzk_registry WHERE bin=$1", "__test_bin_edge__")


@pytest.mark.asyncio
async def test_developers_bin_column_nullable_and_writable(db):
    from bot.db.pg import execute, fetchval, fetchrow
    dev_id = await fetchval(
        "INSERT INTO developers (name) VALUES ('__test_dev_bin__') RETURNING id")
    try:
        row = await fetchrow("SELECT bin FROM developers WHERE id=$1", dev_id)
        assert row["bin"] is None
        await execute("UPDATE developers SET bin=$2 WHERE id=$1", dev_id, "123456789012")
        row = await fetchrow("SELECT bin FROM developers WHERE id=$1", dev_id)
        assert row["bin"] == "123456789012"
    finally:
        await execute("DELETE FROM developers WHERE id=$1", dev_id)
