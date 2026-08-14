"""Регрессия для задачи 2026-08-14 ("House-resolution в скоринге") —
геоцентроид дома (карточка ЖК + /admin/api/complex/{id}/location-score)
должен находить объявления дома ЧЕРЕЗ resolved_house_id, не только по
точному текстовому совпадению имени — house_resolution.py мог привязать
объявление к дому по адресу/токену/гео, пока сам текст объявления всё
ещё называет его именем зонтика."""
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
async def house_resolved_only_listing(client):
    """1 зонтик + 1 дом (parent_complex_id) + 1 apartment_listings, чей
    complex_name всё ещё называет ЗОНТИКА (типичный случай для только
    что расшитого дома), но resolved_house_id уже указывает на ДОМ, с
    координатами, ощутимо отличающимися от зонтика (в тексте зонтика
    вообще нет ни одного объявления по имени — только через resolved_
    house_id можно найти хоть какие-то координаты для дома)."""
    from bot.db.pg import fetchval, execute
    umbrella_id = await fetchval("""
        INSERT INTO complexes (name, is_umbrella) VALUES ('__test_umb_geo__', TRUE) RETURNING id
    """)
    house_id = await fetchval("""
        INSERT INTO complexes (name, parent_complex_id) VALUES ('__test_house_geo__', $1) RETURNING id
    """, umbrella_id)
    await execute("""
        INSERT INTO apartment_listings (id, complex_name, lat, lon, price, area, rooms, resolved_house_id)
        VALUES ('__test_listing_geo_house__', '__test_umb_geo__', 51.15, 71.45, 30000000, 60.0, 2, $1)
    """, house_id)
    try:
        yield umbrella_id, house_id
    finally:
        await execute("DELETE FROM apartment_listings WHERE id = '__test_listing_geo_house__'")
        await execute("DELETE FROM complexes WHERE id IN ($1, $2)", umbrella_id, house_id)


@pytest.mark.asyncio
async def test_house_page_finds_coords_via_resolved_house_id(client, house_resolved_only_listing):
    umbrella_id, house_id = house_resolved_only_listing
    r = await client.get(f"/complex/{house_id}")
    assert r.status_code == 200
    # Локационный скор гейтится наличием geo (см. complex_detail.html,
    # {% if geo %}) — до фикса, без объявлений по точному имени дома,
    # geo был бы None и блок с AJAX-запросом не рендерился бы вовсе.
    assert "location-score" in r.text


@pytest.mark.asyncio
async def test_location_score_api_finds_house_coords_not_no_coords(client, house_resolved_only_listing, monkeypatch):
    # compute_complex_location_score сама бьёт в живой OSM Overpass (сеть) —
    # в этом тесте не про неё (её логика не менялась), про то, что роут
    # ВООБЩЕ до неё доходит с ПРАВИЛЬНЫМИ координатами дома вместо 404
    # no_coords. Подменяем сетевой вызов на дешёвую заглушку, чтобы тест
    # не зависел от доступности Overpass и не ждал 90с таймаут.
    seen = {}

    async def _fake_compute(lat, lon, year_built=None, district=None):
        seen["lat"], seen["lon"] = lat, lon
        return {"total": 0, "factors": {}, "confidence": 0}

    import bot.core.location_score as location_score_module
    monkeypatch.setattr(location_score_module, "compute_complex_location_score", _fake_compute)

    umbrella_id, house_id = house_resolved_only_listing
    r = await client.get(f"/admin/api/complex/{house_id}/location-score")
    assert r.status_code == 200, r.text
    # Координаты — от объявления дома (resolved_house_id), не какие-то
    # чужие/усреднённые по всей базе — единственное объявление в фикстуре
    # стоит на 51.15/71.45.
    assert round(seen["lat"], 2) == 51.15
    assert round(seen["lon"], 2) == 71.45
