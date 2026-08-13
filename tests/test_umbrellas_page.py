"""HTTP-уровневые smoke-тесты — /admin/umbrellas + новые API-эндпойнты
попапов (complex-search, split/by-complex, unset-parent). Задача
2026-08-13 явно потребовала: класс багов "код верный, но не смонтирован
в живом процессе" уже дважды выстрелил в этот же день (404 на split/flag,
0/1241 в krisha_complex_import.py) — только реальный ASGI-запрос через
собранное тем же способом, что service_web.py, приложение это ловит."""
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


@pytest_asyncio.fixture
async def umbrella_fixture(client):
    from bot.db.pg import fetchval, execute
    parent_id = await fetchval(
        "INSERT INTO complexes (name, lat, lon) VALUES ('__test_umb_page_parent__', 51.1, 71.4) RETURNING id")
    child_id = await fetchval("""
        INSERT INTO complexes (name, lat, lon, parent_complex_id)
        VALUES ('__test_umb_page_child__', 51.1, 71.4, $1) RETURNING id
    """, parent_id)
    try:
        yield parent_id, child_id
    finally:
        await execute("DELETE FROM split_candidates WHERE complex_id IN ($1, $2)", parent_id, child_id)
        await execute("DELETE FROM complexes WHERE id IN ($1, $2)", parent_id, child_id)


@pytest.mark.asyncio
async def test_umbrellas_page_lists_umbrella_and_child(client, umbrella_fixture):
    parent_id, child_id = umbrella_fixture
    r = await client.get("/admin/umbrellas")
    assert r.status_code == 200
    assert "__test_umb_page_parent__" in r.text
    assert "__test_umb_page_child__" in r.text
    assert f"umbUnsetParent({child_id})" in r.text


@pytest.mark.asyncio
async def test_umbrellas_page_requires_auth():
    import httpx
    from bot.db.pg import init_pool, close_pool
    from bot.db.compat import BotDB
    from bot.admin_web import create_admin_app
    await init_pool(DATABASE_URL)
    db = BotDB(DB_PATH)
    await db.init()
    app = create_admin_app(db, ADMIN_PASSWORD, "test", DB_PATH)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/admin/umbrellas")
    assert r.status_code in (302, 401)
    await close_pool()


@pytest.mark.asyncio
async def test_complex_search_route_returns_results(client, umbrella_fixture):
    r = await client.get("/admin/api/entity-ids/complex-search?q=__test_umb_page_parent__")
    assert r.status_code == 200
    body = r.json()
    names = [x["name"] for x in body["results"]]
    assert "__test_umb_page_parent__" in names


@pytest.mark.asyncio
async def test_split_by_complex_route(client, umbrella_fixture):
    from bot.db.pg import fetchval
    parent_id, _ = umbrella_fixture
    cid = await fetchval("""
        INSERT INTO split_candidates (complex_id, reason, comment, evidence, matched_by)
        VALUES ($1, 'manual', 'заметка раз', '{}'::jsonb, 'pytest') RETURNING id
    """, parent_id)
    r = await client.get(f"/admin/api/entity-ids/split/by-complex/{parent_id}")
    assert r.status_code == 200
    body = r.json()
    ids = [c["id"] for c in body["candidates"]]
    assert cid in ids
    assert body["candidates"][0]["comment"] == "заметка раз"


@pytest.mark.asyncio
async def test_unset_parent_route(client, umbrella_fixture):
    from bot.db.pg import fetchval
    _, child_id = umbrella_fixture
    r = await client.post("/admin/api/entity-ids/unset-parent", json={"complex_id": child_id})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    got = await fetchval("SELECT parent_complex_id FROM complexes WHERE id=$1", child_id)
    assert got is None


@pytest.mark.asyncio
async def test_unset_parent_route_missing_returns_404(client):
    r = await client.post("/admin/api/entity-ids/unset-parent", json={"complex_id": -1})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_entity_ids_page_renders_merged_source_candidates_section(client):
    """/admin/entity-ids целиком — задача 2026-08-13 ("единая страница
    без якорей"): одна секция source-candidates (не три отдельные
    карточки), общий поиск, попапы вместо якорей/prompt."""
    r = await client.get("/admin/entity-ids")
    assert r.status_code == 200
    assert "Source-candidates" in r.text
    assert 'id="eid-source-search"' in r.text
    assert "eidOpenAcModal" in r.text
    assert "eidOpenNoteModal" in r.text
    assert "eid-split-flag-cid" in r.text  # секция расшивки всё ещё на месте


@pytest.mark.asyncio
async def test_set_parent_route_via_ac_modal_flow(client, umbrella_fixture):
    """Тот же поток, что eidAcPick() в _entity_modals.html — set-parent
    роут (был написан в прошлом заходе, но проверим ещё раз в связке с
    новыми эндпойнтами, раз уж тестируем весь модальный контур разом)."""
    from bot.db.pg import fetchval, execute
    other_id = await fetchval(
        "INSERT INTO complexes (name, lat, lon) VALUES ('__test_umb_ac_child__', 51.1, 71.4) RETURNING id")
    parent_id, _ = umbrella_fixture
    try:
        r = await client.post("/admin/api/entity-ids/set-parent",
                              json={"complex_id": other_id, "parent_complex_id": parent_id})
        assert r.status_code == 200
        got = await fetchval("SELECT parent_complex_id FROM complexes WHERE id=$1", other_id)
        assert got == parent_id
    finally:
        await execute("DELETE FROM complexes WHERE id=$1", other_id)
