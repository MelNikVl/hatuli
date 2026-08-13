"""HTTP-уровневый smoke-тест для POST /admin/api/entity-ids/split/flag
(живой баг 2026-08-13: форма на /admin/entity-ids и на карточке ЖК
возвращала 404 — не потому что путь/метод в коде были неверны
(маршрут был зарегистрирован правильно), а потому что живой процесс
krisha-web.service стартовал ДО коммита, добавившего этот роут —
hot-reload в Python не бывает, см. docs/entity_resolution_plan.md).
Юнит-тесты вызывают flag_split_candidate() напрямую и не ловят ЭТОТ
класс бага (стартуют со свежим кодом всегда) — нужен реальный ASGI-
запрос через FastAPI-приложение, собранное так же, как service_web.py.
"""
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
async def blob_complex(client):
    from bot.db.pg import fetchval, execute
    complex_id = await fetchval(
        "INSERT INTO complexes (name, lat, lon) VALUES ('__test_route_blob__', 51.1, 71.4) RETURNING id")
    try:
        yield complex_id
    finally:
        await execute("DELETE FROM split_candidates WHERE complex_id = $1", complex_id)
        await execute("DELETE FROM complexes WHERE id = $1", complex_id)


@pytest.mark.asyncio
async def test_split_flag_route_registered_and_writes_comment(client, blob_complex):
    """Именно та проверка, которую живой баг бы поймал: путь+метод
    реально смонтированы в приложении (не только определены декоратором
    в файле), запрос доходит до обработчика, обработчик пишет в БД."""
    from bot.db.pg import fetchrow

    comment = "Первая очередь — IV кв 2023, Вторая — III кв 2025, Третья — III кв 2025"
    r = await client.post("/admin/api/entity-ids/split/flag",
                          json={"complex_id": blob_complex, "comment": comment})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True

    row = await fetchrow("SELECT * FROM split_candidates WHERE id=$1", body["id"])
    assert row["complex_id"] == blob_complex
    assert row["comment"] == comment
    assert row["reason"] == "manual"
    assert row["status"] == "review"


@pytest.mark.asyncio
async def test_split_flag_route_repeat_flag_appends_not_4xx(client, blob_complex):
    """UX-фикс 2026-08-13: повторная пометка того же ЖК (пока первая
    не разрешена) — НЕ 4xx (было 409, "стена"), а 200 с id той же
    записи ("мост") + комментарий дописан, не потерян."""
    r1 = await client.post("/admin/api/entity-ids/split/flag",
                           json={"complex_id": blob_complex, "comment": "первая заметка"})
    assert r1.status_code == 200
    first_id = r1.json()["id"]
    assert r1.json()["existing"] is False

    r2 = await client.post("/admin/api/entity-ids/split/flag",
                           json={"complex_id": blob_complex, "comment": "вторая заметка"})
    assert r2.status_code == 200
    assert r2.status_code < 400
    body2 = r2.json()
    assert body2["id"] == first_id
    assert body2["existing"] is True

    from bot.db.pg import fetchrow
    row = await fetchrow("SELECT comment FROM split_candidates WHERE id=$1", first_id)
    assert "первая заметка" in row["comment"]
    assert "вторая заметка" in row["comment"]


@pytest.mark.asyncio
async def test_split_flag_route_requires_auth():
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
        r = await c.post("/admin/api/entity-ids/split/flag", json={"complex_id": 1, "comment": "x"})
    assert r.status_code == 401
    await close_pool()
