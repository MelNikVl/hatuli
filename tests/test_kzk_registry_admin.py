"""Регрессия для задачи 2026-08-15 ("Реестр КЖК"), коммит 4 —
bot/core/kzk_registry_admin.py (сборка данных + confirm/reject/manual-
match) и /admin/kzk-registry + /admin/api/kzk-registry/{id}/* роуты.
Реальная БД, синтетические записи (id-скоуп, не строка) — не трогают
313 реальных строк kzk_registry/514 developers."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import json
import pytest
import pytest_asyncio
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")
DB_PATH = os.getenv("DB_PATH", "bot.db")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")


@pytest_asyncio.fixture
async def db():
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    yield
    await close_pool()


async def _insert_kzk(bin_, legal="ТОО Тест", brand=None, is_blacklisted=False,
                       developer_id=None, method=None):
    from bot.db.pg import fetchval
    return await fetchval(
        "INSERT INTO kzk_registry (bin, developer_legal, developer_brand, in_registry, "
        "is_blacklisted, developer_id, developer_match_method) "
        "VALUES ($1,$2,$3,TRUE,$4,$5,$6) RETURNING id",
        bin_, legal, brand, is_blacklisted, developer_id, method)


async def _insert_developer(name):
    from bot.db.pg import fetchval
    return await fetchval("INSERT INTO developers (name) VALUES ($1) RETURNING id", name)


async def _cleanup_kzk(*ids):
    from bot.db.pg import execute
    await execute("DELETE FROM kzk_registry WHERE id = ANY($1::int[])", list(ids))


async def _cleanup_developers(*ids):
    from bot.db.pg import execute
    await execute("DELETE FROM developers WHERE id = ANY($1::int[])", list(ids))


@pytest.mark.asyncio
async def test_summary_counts_by_status(db):
    from bot.core.kzk_registry_admin import build_kzk_registry_summary

    dev_id = await _insert_developer("__test_admin_dev__")
    ids = [
        await _insert_kzk("__test_kra_resolved__", developer_id=dev_id, method="bin"),
        await _insert_kzk("__test_kra_review__", developer_id=dev_id, method="name_fuzzy_review"),
        await _insert_kzk("__test_kra_unresolved__"),
        await _insert_kzk("__test_kra_blacklist__", is_blacklisted=True),
    ]
    try:
        summary = await build_kzk_registry_summary()
        assert summary["total"] >= 4
        assert summary["resolved"] >= 1
        assert summary["review_pending"] >= 1
        assert summary["unresolved"] >= 1
        assert summary["blacklisted"] >= 1
    finally:
        await _cleanup_kzk(*ids)
        await _cleanup_developers(dev_id)


@pytest.mark.asyncio
async def test_list_filters_by_developer_query(db):
    from bot.core.kzk_registry_admin import list_kzk_registry

    id1 = await _insert_kzk("__test_kra_q1__", brand="UniqueBrandZzzz")
    id2 = await _insert_kzk("__test_kra_q2__", brand="SomethingElse")
    try:
        rows = await list_kzk_registry(developer_query="UniqueBrandZzzz")
        bins = {r["bin"] for r in rows}
        assert "__test_kra_q1__" in bins
        assert "__test_kra_q2__" not in bins
    finally:
        await _cleanup_kzk(id1, id2)


@pytest.mark.asyncio
async def test_list_filters_by_match_status(db):
    from bot.core.kzk_registry_admin import list_kzk_registry

    dev_id = await _insert_developer("__test_admin_dev2__")
    resolved_id = await _insert_kzk("__test_kra_st_resolved__", developer_id=dev_id, method="bin")
    unresolved_id = await _insert_kzk("__test_kra_st_unresolved__")
    try:
        # Без developer_query — фильтр только по match_status, среди ВСЕХ
        # строк (включая 313 реальных) — проверяем ЧЛЕНСТВО наших двух
        # тестовых bin, не изолированный набор (та же логика, что уже
        # применена в test_kzk_registry_collect.py для removed_bins).
        rows = await list_kzk_registry(match_status="unresolved")
        bins = {r["bin"] for r in rows}
        assert "__test_kra_st_unresolved__" in bins
        assert "__test_kra_st_resolved__" not in bins
    finally:
        await _cleanup_kzk(resolved_id, unresolved_id)
        await _cleanup_developers(dev_id)


@pytest.mark.asyncio
async def test_list_blacklisted_only_filter(db):
    from bot.core.kzk_registry_admin import list_kzk_registry

    bl_id = await _insert_kzk("__test_kra_bl1__", brand="BLTestBrand", is_blacklisted=True)
    ok_id = await _insert_kzk("__test_kra_bl2__", brand="BLTestBrand", is_blacklisted=False)
    try:
        rows = await list_kzk_registry(developer_query="BLTestBrand", blacklisted_only=True)
        bins = {r["bin"] for r in rows}
        assert "__test_kra_bl1__" in bins
        assert "__test_kra_bl2__" not in bins
    finally:
        await _cleanup_kzk(bl_id, ok_id)


@pytest.mark.asyncio
async def test_confirm_match_promotes_review_to_manual_confirmed(db):
    from bot.core.kzk_registry_admin import confirm_match
    from bot.db.pg import fetchrow

    dev_id = await _insert_developer("__test_admin_dev3__")
    kzk_id = await _insert_kzk("__test_kra_confirm__", developer_id=dev_id, method="name_fuzzy_review")
    try:
        await confirm_match(kzk_id)
        row = await fetchrow("SELECT developer_match_method, developer_id FROM kzk_registry WHERE id=$1", kzk_id)
        assert row["developer_match_method"] == "manual_confirmed"
        assert row["developer_id"] == dev_id
    finally:
        await _cleanup_kzk(kzk_id)
        await _cleanup_developers(dev_id)


@pytest.mark.asyncio
async def test_confirm_match_without_candidate_raises(db):
    from bot.core.kzk_registry_admin import confirm_match

    kzk_id = await _insert_kzk("__test_kra_noconfirm__")
    try:
        with pytest.raises(ValueError):
            await confirm_match(kzk_id)
    finally:
        await _cleanup_kzk(kzk_id)


@pytest.mark.asyncio
async def test_reject_match_clears_developer(db):
    from bot.core.kzk_registry_admin import reject_match
    from bot.db.pg import fetchrow

    dev_id = await _insert_developer("__test_admin_dev4__")
    kzk_id = await _insert_kzk("__test_kra_reject__", developer_id=dev_id, method="name_fuzzy_review")
    try:
        await reject_match(kzk_id)
        row = await fetchrow("SELECT developer_id, developer_match_method FROM kzk_registry WHERE id=$1", kzk_id)
        assert row["developer_id"] is None
        assert row["developer_match_method"] is None
    finally:
        await _cleanup_kzk(kzk_id)
        await _cleanup_developers(dev_id)


@pytest.mark.asyncio
async def test_set_manual_match_success(db):
    from bot.core.kzk_registry_admin import set_manual_match
    from bot.db.pg import fetchrow

    dev_id = await _insert_developer("__test_admin_dev5__")
    kzk_id = await _insert_kzk("__test_kra_manual__")
    try:
        await set_manual_match(kzk_id, dev_id)
        row = await fetchrow("SELECT developer_id, developer_match_method FROM kzk_registry WHERE id=$1", kzk_id)
        assert row["developer_id"] == dev_id
        assert row["developer_match_method"] == "manual_confirmed"
    finally:
        await _cleanup_kzk(kzk_id)
        await _cleanup_developers(dev_id)


@pytest.mark.asyncio
async def test_set_manual_match_unknown_developer_raises(db):
    from bot.core.kzk_registry_admin import set_manual_match

    kzk_id = await _insert_kzk("__test_kra_manual_bad__")
    try:
        with pytest.raises(ValueError):
            await set_manual_match(kzk_id, 999999999)
    finally:
        await _cleanup_kzk(kzk_id)


@pytest.mark.asyncio
async def test_actions_raise_not_found_for_missing_id(db):
    from bot.core.kzk_registry_admin import confirm_match, reject_match, KzkRegistryNotFound
    with pytest.raises(KzkRegistryNotFound):
        await confirm_match(999999999)
    with pytest.raises(KzkRegistryNotFound):
        await reject_match(999999999)


# ── HTTP-уровень (роуты) ────────────────────────────────────────────────

@pytest_asyncio.fixture
async def client():
    import httpx
    from bot.db.pg import init_pool, close_pool
    from bot.db.compat import BotDB
    from bot.admin_web import create_admin_app

    await init_pool(DATABASE_URL)
    bdb = BotDB(DB_PATH)
    await bdb.init()
    app = create_admin_app(bdb, ADMIN_PASSWORD, "test", DB_PATH)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                 cookies={"admin_auth": "1"}) as c:
        yield c
    await close_pool()


@pytest.mark.asyncio
async def test_page_renders_with_summary_and_rows(client):
    dev_id = await _insert_developer("__test_admin_http_dev__")
    kzk_id = await _insert_kzk("__test_kra_http1__", brand="HttpTestBrand",
                                developer_id=dev_id, method="name_fuzzy_review")
    try:
        r = await client.get("/admin/kzk-registry?q=HttpTestBrand")
        assert r.status_code == 200
        assert "Реестр КЖК" in r.text
        assert "HttpTestBrand" in r.text
        assert "Подтвердить" in r.text  # кнопка для review-кандидата
    finally:
        await _cleanup_kzk(kzk_id)
        await _cleanup_developers(dev_id)


@pytest.mark.asyncio
async def test_confirm_endpoint_updates_row(client):
    dev_id = await _insert_developer("__test_admin_http_dev2__")
    kzk_id = await _insert_kzk("__test_kra_http2__", developer_id=dev_id, method="name_fuzzy_review")
    try:
        r = await client.post(f"/admin/api/kzk-registry/{kzk_id}/confirm")
        assert r.status_code == 200
        assert r.json()["ok"] is True

        from bot.db.pg import fetchrow
        row = await fetchrow("SELECT developer_match_method FROM kzk_registry WHERE id=$1", kzk_id)
        assert row["developer_match_method"] == "manual_confirmed"
    finally:
        await _cleanup_kzk(kzk_id)
        await _cleanup_developers(dev_id)


@pytest.mark.asyncio
async def test_reject_endpoint_returns_404_for_missing(client):
    r = await client.post("/admin/api/kzk-registry/999999999/reject")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_parsers_page_links_to_kzk_registry(client):
    r = await client.get("/admin/parsers")
    assert r.status_code == 200
    assert "/admin/kzk-registry" in r.text
