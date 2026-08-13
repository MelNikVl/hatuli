"""Регрессия search_complexes_for_parent()/unset_parent_complex() —
задача 2026-08-13 ("Зонтики", autocomplete "добавить дом к ЖК"):
транслит + префикс-матч + продуктовые суффиксы (Comfort/Gold/Premium)
не должны мешать совпадению по базовому имени."""
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


@pytest_asyncio.fixture
async def search_fixtures(db):
    from bot.db.pg import fetchval, execute
    ids = []
    for name in ("__test_search_tandau__", "__test_search_тандау__",
                 "__test_search_comfort_tandau__", "__test_search_unrelated__"):
        cid = await fetchval("INSERT INTO complexes (name) VALUES ($1) RETURNING id", name)
        ids.append(cid)
    try:
        yield ids
    finally:
        for cid in ids:
            await execute("DELETE FROM complexes WHERE id=$1", cid)


@pytest.mark.asyncio
async def test_search_finds_transliterated_match(search_fixtures):
    from bot.core.entity_resolution import search_complexes_for_parent
    results = await search_complexes_for_parent("__test_search_тандау__")
    names = [r["name"] for r in results]
    assert "__test_search_tandau__" in names  # латиница найдена по кириллическому запросу


@pytest.mark.asyncio
async def test_search_ignores_product_token_for_base_match(search_fixtures):
    """"Comfort" в имени не должен мешать найти по базовому "tandau"."""
    from bot.core.entity_resolution import search_complexes_for_parent
    results = await search_complexes_for_parent("__test_search_tandau__")
    names = [r["name"] for r in results]
    assert "__test_search_comfort_tandau__" in names


@pytest.mark.asyncio
async def test_search_excludes_self(search_fixtures):
    from bot.core.entity_resolution import search_complexes_for_parent
    cid = search_fixtures[0]
    results = await search_complexes_for_parent("__test_search_tandau__", exclude_id=cid)
    ids = [r["id"] for r in results]
    assert cid not in ids


@pytest.mark.asyncio
async def test_search_short_query_returns_empty(db):
    from bot.core.entity_resolution import search_complexes_for_parent
    assert await search_complexes_for_parent("a") == []


@pytest.mark.asyncio
async def test_unset_parent_complex(db):
    from bot.core.entity_resolution import set_parent_complex, unset_parent_complex
    from bot.db.pg import fetchval, execute
    parent_id = await fetchval("INSERT INTO complexes (name) VALUES ('__test_unset_parent__') RETURNING id")
    child_id = await fetchval("INSERT INTO complexes (name) VALUES ('__test_unset_child__') RETURNING id")
    try:
        await set_parent_complex(child_id, parent_id, "pytest")
        got = await fetchval("SELECT parent_complex_id FROM complexes WHERE id=$1", child_id)
        assert got == parent_id

        result = await unset_parent_complex(child_id)
        assert result == {"complex_id": child_id}
        got2 = await fetchval("SELECT parent_complex_id FROM complexes WHERE id=$1", child_id)
        assert got2 is None
    finally:
        await execute("DELETE FROM complexes WHERE id IN ($1, $2)", parent_id, child_id)


@pytest.mark.asyncio
async def test_unset_parent_complex_missing_returns_none(db):
    from bot.core.entity_resolution import unset_parent_complex
    assert await unset_parent_complex(-1) is None
