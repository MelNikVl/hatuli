"""Регрессия для задачи 2026-08-14 (Г12, docs/data_collection_audit.md):
/admin/dashboard/data должен показывать investment-статистику из живого
писателя (SQLite bot.db), не из мёртвого снимка Postgres investment_
listings (мигрирован разово migrate_sqlite_to_pg.py 2026-06-05, с тех пор
не обновлялся — до фикса дашборд молча показывал 2-месячную давность как
текущую)."""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")


@pytest_asyncio.fixture
async def sqlite_db_path():
    import aiosqlite
    from bot.db.models import init_investment_table

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    await init_investment_table(path)
    now = datetime.now(timezone.utc)
    async with aiosqlite.connect(path) as db:
        # 1 свежая (сегодня) + 1 старая (30 дней назад) запись.
        await db.execute(
            "INSERT INTO investment_listings (id, price, score_total, first_seen, last_seen) "
            "VALUES ('inv-fresh', 3000000, 82, ?, ?)",
            (now.isoformat(), now.isoformat()))
        await db.execute(
            "INSERT INTO investment_listings (id, price, score_total, first_seen, last_seen) "
            "VALUES ('inv-old', 2000000, 55, ?, ?)",
            ((now - timedelta(days=30)).isoformat(), (now - timedelta(days=30)).isoformat()))
        await db.commit()
    yield path
    os.unlink(path)


@pytest.mark.asyncio
async def test_get_healthcheck_stats_reads_live_sqlite(sqlite_db_path):
    from bot.db.investment_queries import get_healthcheck_stats

    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    stats = await get_healthcheck_stats(sqlite_db_path, today_start.isoformat())
    assert stats["total"] == 2
    assert stats["today"] == 1  # только 'inv-fresh' — сегодня
    assert stats["top_score"] == 82
    assert stats["last_found"] is not None


@pytest.mark.asyncio
async def test_get_healthcheck_stats_empty_db_returns_zeros():
    import aiosqlite
    from bot.db.models import init_investment_table
    from bot.db.investment_queries import get_healthcheck_stats

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        await init_investment_table(path)
        stats = await get_healthcheck_stats(path, datetime.now(timezone.utc).isoformat())
        assert stats == {"total": 0, "today": 0, "top_score": 0, "last_found": None}
    finally:
        os.unlink(path)


@pytest.mark.asyncio
async def test_dashboard_data_route_uses_sqlite_investment_stats(sqlite_db_path):
    """HTTP-уровень: /admin/dashboard/data с реально подставленным
    db_path — ключевая регрессия: раньше этот роут читал investment_
    listings из Postgres (пусто/протухло в тестовой БД) НЕЗАВИСИМО от
    того, что реально лежит в SQLite, который ему передали."""
    import httpx
    from bot.db.pg import init_pool, close_pool
    from bot.db.compat import BotDB
    from bot.admin_web import create_admin_app

    await init_pool(DATABASE_URL)
    db = BotDB(sqlite_db_path)
    await db.init()
    app = create_admin_app(db, ADMIN_PASSWORD, "test", sqlite_db_path)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                 cookies={"admin_auth": "1"}) as c:
        r = await c.get("/admin/dashboard/data")
    await close_pool()

    assert r.status_code == 200
    body = r.json()
    assert body["investment"]["total"] == 2
    assert body["investment"]["today"] == 1
    assert body["investment"]["top_score"] == 82
    assert body["db"]["investment_count"] == 2
