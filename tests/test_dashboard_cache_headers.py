"""tests/test_dashboard_cache_headers.py — регрессия для живого бага
2026-08-22 ("скролл в правой панели до выбранного объявления снова
сломался"). Реальная причина не воспроизводилась на прямом запросе к
origin-серверу (мимо CDN/браузерного кэша) — потому что HTML самой
страницы (в отличие от /admin/api/*, см. соседний no-store-миддлвар в
bot/admin_web.py, добавленный ПОСЛЕ такого же бага с Cloudflare и
устаревшими JSON-ответами) отдавался БЕЗ Cache-Control: Cloudflare/
браузер мог закэшировать HTML целиком (включая инлайновый <script> с
логикой карты и панели) и продолжать отдавать старую версию уже после
того, как на сервере всё исправлено.

bot/admin_web.py::_render_dashboard теперь всегда отдаёт
Cache-Control: no-store, max-age=0 — та же страница, что рендерится из
живых данных (stats/tier/listing_id) при каждом запросе, кэшировать её
не должно быть смысла ни браузеру, ни CDN."""
from __future__ import annotations

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
    bdb = BotDB(DB_PATH)
    await bdb.init()
    app = create_admin_app(bdb, ADMIN_PASSWORD, "test", DB_PATH)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                  cookies={"admin_auth": "1"}) as c:
        yield c
    await close_pool()


@pytest.mark.asyncio
async def test_root_dashboard_is_never_cached(client):
    r = await client.get("/")
    assert r.status_code == 200
    cc = r.headers.get("cache-control", "")
    assert "no-store" in cc


@pytest.mark.asyncio
async def test_admin_dashboard_is_never_cached(client):
    r = await client.get("/admin")
    assert r.status_code == 200
    assert "no-store" in r.headers.get("cache-control", "")


@pytest.mark.asyncio
async def test_listing_page_is_never_cached(client):
    """Прямая ссылка на объявление (/listing/{id}, попап открыт сразу) —
    тот же _render_dashboard, тот же HTML со скриптом карты/панели."""
    r = await client.get("/listing/__does_not_exist_cache_test__")
    assert r.status_code == 200
    assert "no-store" in r.headers.get("cache-control", "")
