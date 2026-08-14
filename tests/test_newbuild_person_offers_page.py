"""HTTP-смоук на /complex/{id} для новостроек — задача 2026-08-14, "двойное
размещение предложений людей в новостройках": секция "Предложения людей
(Крыша)" с тегом переуступка/вторичка, вместо обычной "Объявления в этом
доме" (та остаётся для вторички без изменений). Тот же паттерн реального
ASGI-запроса, что tests/test_complex_detail_route.py."""
import os
import sys
from datetime import datetime, timezone

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
async def newbuild_with_person_offers(client):
    """1 is_newbuild ЖК (сдача В ПРОШЛОМ году — дом уже сдан) + 1 newbuild_units
    (официальный юнит застройщика, 500000 ₸/м²) + 2 apartment_listings того
    же ЖК: один с "переуступка" в тексте (тег assignment — текстовый сигнал
    сильнее даты, даже у уже сданного дома), один обычный без спецсигнала
    в тексте, first_seen СЕЙЧАС — заведомо после (прошлогоднего) срока
    сдачи, тег resale."""
    from bot.db.pg import fetchval, execute
    dev_id = await fetchval(
        "INSERT INTO developers (name) VALUES ('__test_dev_person_offers__') RETURNING id")
    past_year = datetime.now(timezone.utc).year - 1
    complex_id = await fetchval("""
        INSERT INTO complexes (name, lat, lon, developer_id, is_newbuild, completion_year, completion_quarter)
        VALUES ('__test_nb_person_offers__', 51.1, 71.4, $1, TRUE, $2, 1) RETURNING id
    """, dev_id, past_year)
    unit_id = await fetchval("""
        INSERT INTO newbuild_units (complex_id, source, source_unit_id, rooms, area, price, price_per_m2, status)
        VALUES ($1, 'bazis', '__test_unit_po__', 2, 60.0, 30000000, 500000, 'available') RETURNING id
    """, complex_id)
    await execute("""
        INSERT INTO apartment_listings (id, complex_name, title, description, price, area, rooms, first_seen)
        VALUES ('__test_listing_assignment__', '__test_nb_person_offers__',
                'Переуступка 2-комнатной квартиры', 'ДДУ, переуступка прав', 33000000, 60.0, 2, now())
    """)
    await execute("""
        INSERT INTO apartment_listings (id, complex_name, title, description, price, area, rooms, first_seen)
        VALUES ('__test_listing_resale__', '__test_nb_person_offers__',
                'Продам квартиру в готовом доме', 'без ремонта', 27000000, 60.0, 2, now())
    """)
    try:
        yield complex_id, unit_id
    finally:
        await execute("DELETE FROM unit_source_links WHERE unit_id = $1", unit_id)
        await execute("DELETE FROM apartment_listings WHERE id IN "
                       "('__test_listing_assignment__', '__test_listing_resale__')")
        await execute("DELETE FROM newbuild_units WHERE id = $1", unit_id)
        await execute("DELETE FROM complexes WHERE id = $1", complex_id)
        await execute("DELETE FROM developers WHERE id = $1", dev_id)


@pytest.mark.asyncio
async def test_newbuild_page_shows_person_offers_section(client, newbuild_with_person_offers):
    complex_id, unit_id = newbuild_with_person_offers
    r = await client.get(f"/complex/{complex_id}")
    assert r.status_code == 200
    assert "Предложения людей (Крыша)" in r.text
    # "Объявления в этом доме" (простая секция для вторички) на странице
    # новостройки не должна дублироваться рядом с богатой секцией.
    assert "📋 Объявления в этом доме" not in r.text


@pytest.mark.asyncio
async def test_newbuild_page_lists_both_person_offers(client, newbuild_with_person_offers):
    # Точные значения тега проверяются через JSON-эндпоинт ниже (raw HTML
    # страницы содержит слова "переуступка"/"вторичка" ещё и в собственных
    # комментариях шаблона — substring-проверка тега на самой HTML-странице
    # была бы ненадёжной).
    complex_id, unit_id = newbuild_with_person_offers
    r = await client.get(f"/complex/{complex_id}")
    assert r.status_code == 200
    assert "/listing/__test_listing_assignment__" in r.text
    assert "/listing/__test_listing_resale__" in r.text


@pytest.mark.asyncio
async def test_newbuild_map_units_endpoint_includes_person_offers_with_badge(client, newbuild_with_person_offers):
    complex_id, unit_id = newbuild_with_person_offers
    r = await client.get(f"/admin/api/newbuild-complex/{complex_id}/units")
    assert r.status_code == 200
    body = r.json()
    sources = {u["id"]: u.get("source") for u in body["units"]}
    assert sources.get("__test_listing_assignment__") == "person"
    assert sources.get("__test_listing_resale__") == "person"
    dev_units = [u for u in body["units"] if u["source"] == "developer"]
    assert any(u["id"] == unit_id for u in dev_units)
    person_units = {u["id"]: u for u in body["units"] if u["source"] == "person"}
    assert person_units["__test_listing_assignment__"]["tag"] == "assignment"
    assert person_units["__test_listing_assignment__"]["tag_signal"] == "text"
    assert person_units["__test_listing_resale__"]["tag"] == "resale"
    assert person_units["__test_listing_resale__"]["tag_signal"] == "date"
    # каждое apartment_listings.id встречается ровно один раз в ответе
    # (задача "аналитика считает listing один раз")
    person_ids = [u["id"] for u in body["units"] if u["source"] == "person"]
    assert len(person_ids) == len(set(person_ids))
