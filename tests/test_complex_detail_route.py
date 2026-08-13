"""HTTP-смоук на /complex/{id} — "модель зонтик/дом" (задача 2026-08-13):
дом ссылается на зонтик, зонтик перечисляет дома, кнопка расшивки
меняет ярлык, если на ЖК уже есть неразрешённая пометка. Тот же паттерн,
что tests/test_split_flag_route.py (реальный ASGI-запрос, не вызов
функции напрямую — ловит и разметку, и то, что роут реально отдаёт 200
на новых полях контекста, которые могли не пробрасываться)."""
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
async def umbrella_and_house(client):
    from bot.db.pg import fetchval, execute
    umbrella_id = await fetchval(
        "INSERT INTO complexes (name, lat, lon) VALUES ('__test_umbrella_page__', 51.1, 71.4) RETURNING id")
    house_id = await fetchval("""
        INSERT INTO complexes (name, lat, lon, parent_complex_id)
        VALUES ('__test_house_page__ A', 51.1, 71.4, $1) RETURNING id
    """, umbrella_id)
    try:
        yield umbrella_id, house_id
    finally:
        await execute("DELETE FROM split_candidates WHERE complex_id IN ($1, $2)", umbrella_id, house_id)
        await execute("DELETE FROM complexes WHERE id IN ($1, $2)", umbrella_id, house_id)


@pytest.mark.asyncio
async def test_house_page_links_to_parent(client, umbrella_and_house):
    umbrella_id, house_id = umbrella_and_house
    r = await client.get(f"/complex/{house_id}")
    assert r.status_code == 200
    assert "Часть комплекса" in r.text
    assert f"/complex/{umbrella_id}" in r.text


@pytest.mark.asyncio
async def test_umbrella_page_lists_houses(client, umbrella_and_house):
    umbrella_id, house_id = umbrella_and_house
    r = await client.get(f"/complex/{umbrella_id}")
    assert r.status_code == 200
    assert "Дома в составе комплекса" in r.text
    assert "__test_house_page__ A" in r.text
    assert f"/complex/{house_id}" in r.text


@pytest.mark.asyncio
async def test_plain_complex_shows_flag_button_not_open_note(client, umbrella_and_house):
    umbrella_id, _ = umbrella_and_house
    r = await client.get(f"/complex/{umbrella_id}")
    assert "Пометить на расшивку" in r.text
    assert "Открыть пометку" not in r.text


@pytest.mark.asyncio
async def test_complex_with_pending_candidate_shows_open_note(client, umbrella_and_house):
    """UX-фикс 2026-08-13: если уже есть неразрешённая пометка — кнопка
    меняет ярлык на "Открыть пометку" со ссылкой-якорем, не показывает
    исходную кнопку заново."""
    from bot.db.pg import fetchval
    umbrella_id, _ = umbrella_and_house
    cid = await fetchval("""
        INSERT INTO split_candidates (complex_id, reason, comment, evidence, matched_by)
        VALUES ($1, 'manual', 'заметка', '{}'::jsonb, 'pytest') RETURNING id
    """, umbrella_id)

    r = await client.get(f"/complex/{umbrella_id}")
    assert "Открыть пометку" in r.text
    assert f"eid-split-{cid}" in r.text
    assert "Пометить на расшивку" not in r.text


@pytest.mark.asyncio
async def test_homeportal_photos_removed_from_official_data_block(client, umbrella_and_house):
    """Задача 2026-08-13: убрать фото из блока "Официальные данные
    (homeportal.kz)" — cxOpenLightboxByUrl (только там использовался)
    больше не должен встречаться в разметке."""
    umbrella_id, _ = umbrella_and_house
    r = await client.get(f"/complex/{umbrella_id}")
    assert "cxOpenLightboxByUrl" not in r.text
