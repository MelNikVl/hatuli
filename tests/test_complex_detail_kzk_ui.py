"""HTTP-смоук для задачи 2026-08-15 ("БВУ/КЖК/МИО в карточках ЖК") —
блок #cx-kzk-block на /complex/{id}: реальный ASGI-запрос (тот же
паттерн, что tests/test_complex_detail_route.py), не только unit-тест
get_kzk_info() (см. tests/test_complex_detail_kzk_info.py)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")
DB_PATH = os.getenv("DB_PATH", "bot.db")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")


@pytest_asyncio.fixture
async def client():
    import httpx
    from bot.db.pg import init_pool, close_pool
    from bot.db.compat import BotDB
    from bot.admin_web import create_admin_app

    await init_pool(DATABASE_URL)
    db = BotDB(DB_PATH)
    await db.init()
    app = create_admin_app(db, ADMIN_PASSWORD, "test", DB_PATH)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                 cookies={"admin_auth": "1"}) as c:
        yield c
    await close_pool()


async def _insert_complex(name, is_newbuild):
    from bot.db.pg import fetchval
    return await fetchval(
        "INSERT INTO complexes (name, is_newbuild) VALUES ($1, $2) RETURNING id",
        name, is_newbuild)


async def _insert_tech_specs(complex_id, developer_bin):
    from bot.db.pg import execute
    await execute(
        "INSERT INTO complex_tech_specs (complex_id, developer_bin) VALUES ($1, $2)",
        complex_id, developer_bin)


async def _insert_kzk(bin_, warranty_scheme=None, is_blacklisted=False):
    from bot.db.pg import execute
    await execute(
        "INSERT INTO kzk_registry (bin, developer_legal, warranty_scheme, is_blacklisted, in_registry) "
        "VALUES ($1, 'ТОО Тест', $2, $3, TRUE)",
        bin_, warranty_scheme, is_blacklisted)


async def _cleanup(cid, bin_=None):
    from bot.db.pg import execute
    await execute("DELETE FROM complex_tech_specs WHERE complex_id=$1", cid)
    await execute("DELETE FROM complexes WHERE id=$1", cid)
    if bin_:
        await execute("DELETE FROM kzk_registry WHERE bin=$1", bin_)


@pytest.mark.asyncio
async def test_kzk_block_shown_for_newbuild_with_kzk_guarantee(client):
    cid = await _insert_complex("__test_kzkui_nb1__", True)
    await _insert_tech_specs(cid, "__test_kzkui_bin1__")
    await _insert_kzk("__test_kzkui_bin1__", warranty_scheme="Гарантия КЖК")
    try:
        r = await client.get(f"/complex/{cid}")
        assert r.status_code == 200
        assert "Юридическая защита дольщика" in r.text
        assert "Гарантия КЖК" in r.text
    finally:
        await _cleanup(cid, "__test_kzkui_bin1__")


@pytest.mark.asyncio
async def test_kzk_block_hidden_for_blacklisted_developer(client):
    cid = await _insert_complex("__test_kzkui_nb2__", True)
    await _insert_tech_specs(cid, "__test_kzkui_bin2__")
    await _insert_kzk("__test_kzkui_bin2__", warranty_scheme="Участие БВУ", is_blacklisted=True)
    try:
        r = await client.get(f"/complex/{cid}")
        assert r.status_code == 200
        assert "В чёрном списке КЖК" in r.text
    finally:
        await _cleanup(cid, "__test_kzkui_bin2__")


@pytest.mark.asyncio
async def test_kzk_block_hidden_for_secondary_market(client):
    cid = await _insert_complex("__test_kzkui_sec__", False)
    await _insert_tech_specs(cid, "__test_kzkui_bin3__")
    await _insert_kzk("__test_kzkui_bin3__", warranty_scheme="Гарантия КЖК")
    try:
        r = await client.get(f"/complex/{cid}")
        assert r.status_code == 200
        assert "cx-kzk-block" not in r.text  # is_newbuild=False
    finally:
        await _cleanup(cid, "__test_kzkui_bin3__")


@pytest.mark.asyncio
async def test_kzk_block_hidden_when_no_match(client):
    cid = await _insert_complex("__test_kzkui_nomatch__", True)
    try:
        r = await client.get(f"/complex/{cid}")
        assert r.status_code == 200
        assert "cx-kzk-block" not in r.text
    finally:
        await _cleanup(cid)
