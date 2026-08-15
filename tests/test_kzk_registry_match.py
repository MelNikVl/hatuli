"""Регрессия для задачи 2026-08-15 ("Реестр КЖК"), коммит 3 —
kzk_registry_match.py: двухуровневый matching (БИН/fuzzy-имя на
уровень застройщика через bot/core/entity_resolution пороги, fuzzy на
уровень ЖК по редким zhk_names). Реальная БД, тот же паттерн, что
остальные тесты этой сессии — все запросы СКОУПЛЕНЫ через kzk_ids/
complex_ids синтетических тестовых записей (id, не строки), не трогают
313 реальных записей kzk_registry/514 developers/2131 complexes."""
import os
import sys

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


async def _insert_kzk(bin_, legal, brand=None, zhk_names=None):
    from bot.db.pg import fetchval
    return await fetchval(
        "INSERT INTO kzk_registry (bin, developer_legal, developer_brand, in_registry, zhk_names) "
        "VALUES ($1, $2, $3, TRUE, $4::jsonb) RETURNING id",
        bin_, legal, brand, json.dumps(zhk_names) if zhk_names is not None else None)


async def _insert_developer(name, bin_=None, aliases=None):
    from bot.db.pg import fetchval
    return await fetchval(
        "INSERT INTO developers (name, bin, aliases) VALUES ($1, $2, $3) RETURNING id",
        name, bin_, aliases)


async def _insert_complex(name):
    from bot.db.pg import fetchval
    return await fetchval("INSERT INTO complexes (name) VALUES ($1) RETURNING id", name)


async def _cleanup_kzk(*ids):
    from bot.db.pg import execute
    await execute("DELETE FROM kzk_registry WHERE id = ANY($1::int[])", list(ids))


async def _cleanup_developers(*ids):
    from bot.db.pg import execute
    await execute("DELETE FROM developers WHERE id = ANY($1::int[])", list(ids))


async def _cleanup_complexes(*ids):
    from bot.db.pg import execute
    await execute("DELETE FROM complexes WHERE id = ANY($1::int[])", list(ids))


@pytest.mark.asyncio
async def test_match_developers_by_bin_exact(db):
    """БИН — приоритет 1, обходит fuzzy вовсе (даже если имена не похожи)."""
    from kzk_registry_match import match_developers
    from bot.db.pg import fetchrow

    dev_id = await _insert_developer("Совершенно другое имя", bin_="__test_bin_exact__")
    kzk_id = await _insert_kzk("__test_bin_exact__", "ТОО Ничего общего")
    try:
        result = await match_developers(kzk_ids=[kzk_id])
        assert result["bin"] == 1
        row = await fetchrow("SELECT developer_id, developer_match_method FROM kzk_registry WHERE id=$1", kzk_id)
        assert row["developer_id"] == dev_id
        assert row["developer_match_method"] == "bin"
    finally:
        await _cleanup_kzk(kzk_id)
        await _cleanup_developers(dev_id)


@pytest.mark.asyncio
async def test_match_developers_fuzzy_auto_on_close_name(db):
    from kzk_registry_match import match_developers
    from bot.db.pg import fetchrow

    dev_id = await _insert_developer("Almaty Stroy Invest Company")
    kzk_id = await _insert_kzk("__test_bin_fuzzy1__", "Almaty Stroy Invest Compani", brand="Almaty Stroy")
    try:
        result = await match_developers(kzk_ids=[kzk_id])
        row = await fetchrow("SELECT developer_id, developer_match_method FROM kzk_registry WHERE id=$1", kzk_id)
        assert row["developer_match_method"] in ("name_fuzzy_auto", "name_fuzzy_review")
        assert row["developer_id"] == dev_id
        assert result["total"] == 1
    finally:
        await _cleanup_kzk(kzk_id)
        await _cleanup_developers(dev_id)


@pytest.mark.asyncio
async def test_match_developers_no_match_unresolved(db):
    from kzk_registry_match import match_developers
    from bot.db.pg import fetchrow

    dev_id = await _insert_developer("Совершенно Иная Компания Строй")
    kzk_id = await _insert_kzk("__test_bin_none__", "Полностью Другое Название Zxq")
    try:
        result = await match_developers(kzk_ids=[kzk_id])
        assert result["unresolved"] == 1
        row = await fetchrow("SELECT developer_id, developer_match_method FROM kzk_registry WHERE id=$1", kzk_id)
        assert row["developer_id"] is None
        assert row["developer_match_method"] is None
    finally:
        await _cleanup_kzk(kzk_id)
        await _cleanup_developers(dev_id)


@pytest.mark.asyncio
async def test_match_developers_uses_aliases(db):
    """Матч через aliases, не только через name напрямую."""
    from kzk_registry_match import match_developers
    from bot.db.pg import fetchrow

    dev_id = await _insert_developer("__test_dev_MAINBRAND__", aliases=["__test_alias_secondary__"])
    kzk_id = await _insert_kzk("__test_bin_alias__", "__test_alias_secondary__ TOO")
    try:
        await match_developers(kzk_ids=[kzk_id])
        row = await fetchrow("SELECT developer_id FROM kzk_registry WHERE id=$1", kzk_id)
        assert row["developer_id"] == dev_id
    finally:
        await _cleanup_kzk(kzk_id)
        await _cleanup_developers(dev_id)


@pytest.mark.asyncio
async def test_match_developers_skips_when_no_brand_or_legal(db):
    from kzk_registry_match import match_developers
    from bot.db.pg import execute, fetchval

    kzk_id = await fetchval(
        "INSERT INTO kzk_registry (bin, developer_legal, in_registry) VALUES ($1, '', TRUE) RETURNING id",
        "__test_bin_empty__")
    try:
        result = await match_developers(kzk_ids=[kzk_id])
        assert result["unresolved"] == 1
    finally:
        await _cleanup_kzk(kzk_id)


@pytest.mark.asyncio
async def test_match_zhk_names_auto_match(db):
    from kzk_registry_match import match_zhk_names
    from bot.db.pg import fetchrow

    complex_id = await _insert_complex("ЖК Тестовый Кристалл Плюс")
    kzk_id = await _insert_kzk("__test_bin_zhk1__", "ТОО Тест", zhk_names=["ЖК Тестовый Кристалл Плюс"])
    try:
        result = await match_zhk_names(kzk_ids=[kzk_id])
        assert result["matched"] == 1
        row = await fetchrow("SELECT zhk_matches FROM kzk_registry WHERE id=$1", kzk_id)
        matches = row["zhk_matches"]
        matches = json.loads(matches) if isinstance(matches, str) else matches
        assert len(matches) == 1
        assert matches[0]["complex_id"] == complex_id
        assert matches[0]["method"] == "auto"
    finally:
        await _cleanup_kzk(kzk_id)
        await _cleanup_complexes(complex_id)


@pytest.mark.asyncio
async def test_match_zhk_names_no_match_records_null_complex_id(db):
    from kzk_registry_match import match_zhk_names
    from bot.db.pg import fetchrow

    kzk_id = await _insert_kzk("__test_bin_zhk2__", "ТОО Тест", zhk_names=["ЖК Совершенно Иное Название Qzx"])
    try:
        result = await match_zhk_names(kzk_ids=[kzk_id])
        assert result["pending"] >= 1
        row = await fetchrow("SELECT zhk_matches FROM kzk_registry WHERE id=$1", kzk_id)
        matches = row["zhk_matches"]
        matches = json.loads(matches) if isinstance(matches, str) else matches
        assert matches[0]["complex_id"] is None
    finally:
        await _cleanup_kzk(kzk_id)


@pytest.mark.asyncio
async def test_match_zhk_names_skips_empty_array(db):
    from kzk_registry_match import match_zhk_names

    kzk_id = await _insert_kzk("__test_bin_zhk3__", "ТОО Тест", zhk_names=[])
    try:
        result = await match_zhk_names(kzk_ids=[kzk_id])
        assert result["total_records"] == 0  # пустой массив не считается "есть zhk_names"
    finally:
        await _cleanup_kzk(kzk_id)


@pytest.mark.asyncio
async def test_match_kzk_to_complexes_runs_both_levels(db):
    from kzk_registry_match import match_kzk_to_complexes

    dev_id = await _insert_developer("Оркестратор Тест Компани")
    complex_id = await _insert_complex("ЖК Оркестратор Тест Плаза")
    kzk_id = await _insert_kzk("__test_bin_orch__", "Оркестратор Тест Компани",
                                zhk_names=["ЖК Оркестратор Тест Плаза"])
    try:
        result = await match_kzk_to_complexes(kzk_ids=[kzk_id])
        assert "developers" in result and "zhk" in result
        assert result["developers"]["total"] == 1
        assert result["zhk"]["total_records"] == 1
    finally:
        await _cleanup_kzk(kzk_id)
        await _cleanup_developers(dev_id)
        await _cleanup_complexes(complex_id)
