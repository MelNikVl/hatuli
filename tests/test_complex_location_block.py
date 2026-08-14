"""Регрессия для Фазы L2 продуктового трека «Локация» (docs/location_
product_design.md, план L2, задача 2026-08-14), коммит 2 — новый блок
«🗺 Локация» на /complex/{id} (bot/templates/complex_detail.html).
HTTP-смоук, тот же паттерн, что tests/test_complex_detail_route.py:
реальный ASGI-запрос, ловит и разметку, и то, что роут реально
рендерит новый блок при заданных координатах.

Не проверяет JS-логику (только серверную разметку/условия) — фактическое
поведение fetch()/группировки покрыто bot/core/complex_location_detail.py
тестами (tests/test_complex_location_detail.py), это НЕ дублирует их."""
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
async def complex_with_geo(client):
    from bot.db.pg import fetchval, execute
    cid = await fetchval("INSERT INTO complexes (name) VALUES ('__test_locblock_geo__') RETURNING id")
    lid = "__test_locblock_listing__"
    await execute(
        "INSERT INTO apartment_listings (id, complex_name, lat, lon, price, area, rooms, is_active) "
        "VALUES ($1, '__test_locblock_geo__', 51.14, 71.44, 30000000, 60.0, 2, TRUE)", lid)
    try:
        yield cid
    finally:
        await execute("DELETE FROM apartment_listings WHERE id=$1", lid)
        await execute("DELETE FROM complexes WHERE id=$1", cid)


@pytest_asyncio.fixture
async def complex_without_geo(client):
    from bot.db.pg import fetchval, execute
    cid = await fetchval("INSERT INTO complexes (name) VALUES ('__test_locblock_nogeo__') RETURNING id")
    try:
        yield cid
    finally:
        await execute("DELETE FROM complexes WHERE id=$1", cid)


@pytest.mark.asyncio
async def test_location_block_renders_when_geo_present(client, complex_with_geo):
    r = await client.get(f"/complex/{complex_with_geo}")
    assert r.status_code == 200
    assert "cx-location-block" in r.text
    assert "🗺 Локация" in r.text
    assert f"/admin/api/complex/{complex_with_geo}/location-detail" in r.text
    assert "cxl-groups" in r.text
    assert "cxl-risk-badges" in r.text
    assert "cxl-no-score" in r.text


@pytest.mark.asyncio
async def test_location_block_absent_when_no_geo(client, complex_without_geo):
    """Тот же гейт, что уже действует для существующей карты-точки/
    карточки "Что рядом" (`{% if geo %}`) — без резолвящихся координат
    новый блок не рендерится вовсе, не рендерится пустым."""
    r = await client.get(f"/complex/{complex_without_geo}")
    assert r.status_code == 200
    assert "cx-location-block" not in r.text


@pytest.mark.asyncio
async def test_location_block_does_not_alter_existing_what_is_nearby_card(client, complex_with_geo):
    """План L2 п.4: существующая карточка "Что рядом" не трогается —
    её элементы (cx-card-location, cx-loc-score-card) и вызов старого
    /location-score остаются на странице как были."""
    r = await client.get(f"/complex/{complex_with_geo}")
    assert "cx-card-location" in r.text
    assert "cx-loc-score-card" in r.text
    assert f"/admin/api/complex/{complex_with_geo}/location-score" in r.text


@pytest.mark.asyncio
async def test_location_block_map_layers_reuse_existing_leaflet_map(client, complex_with_geo):
    """Коммит 3 плана L2: слои карты переиспользуют window.cxLocMap
    (карта-точка .cx-row2), не создают второй Leaflet instance — сама
    JS-логика (cxlBuildMapLayers) не выполняется в этом HTTP-смоуке
    (нет браузера), проверяем, что нужная разметка/функция реально
    отдаётся сервером."""
    r = await client.get(f"/complex/{complex_with_geo}")
    assert "window.cxLocMap" in r.text
    assert "cxlBuildMapLayers" in r.text
    # 3 тумблера ровно как в плане L2 п.3: POI / Снос / Плотность
    assert "📍 POI" in r.text
    assert "🚧 Снос" in r.text
    assert "🌡 Плотность" in r.text
