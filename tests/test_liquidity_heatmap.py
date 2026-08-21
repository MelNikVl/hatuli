"""tests/test_liquidity_heatmap.py — /admin/api/liquidity-points (задача
2026-08-21, тепловая карта "Скорость ухода с рынка" на главной странице).
Синтетические фикстуры, реальная Postgres test DB (DATABASE_URL) — тот же
паттерн, что tests/test_market_dashboards.py."""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")


@pytest_asyncio.fixture
async def db():
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    yield
    await close_pool()


@pytest.mark.asyncio
async def test_liquidity_points_public_and_shaped(db):
    """Публичный (без логина) эндпойнт, отдаёт lat/lon/days только для
    подтверждённо выбывших листингов с известным time_on_market —
    активные (censored, time_on_market IS NULL) не подмешиваются."""
    from bot.db.pg import execute
    suffix = uuid.uuid4().hex[:8]
    lid = f"__test_liq_{suffix}__"
    now = datetime.now(timezone.utc)
    try:
        await execute(
            """
            INSERT INTO apartment_listings (id, url, price, area, rooms, lat, lon,
                                             is_active, first_seen, last_seen, archived_at, archive_reason)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
            """,
            lid, f"https://krisha.kz/test/{lid}", 25_000_000, 45.0, 2,
            51.09, 71.42, False, now - timedelta(days=20), now - timedelta(days=5),
            now - timedelta(days=5), "confirmed_gone",
        )
        await execute(
            "INSERT INTO outcome_labels (listing_id, time_on_market) VALUES ($1, $2)",
            lid, 15,
        )

        from bot.admin_web import create_admin_app
        from bot.db.compat import BotDB
        from httpx import AsyncClient, ASGITransport
        app = create_admin_app(BotDB("/tmp/__test_liq_admin.db"), admin_password="x", bot_version="test")
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Без cookie admin_auth — эндпойнт публичный (карта на главной
            # доступна без логина).
            r = await client.get("/admin/api/liquidity-points")
            assert r.status_code == 200
            data = r.json()
            assert "points" in data and isinstance(data["points"], list)
            match = [p for p in data["points"] if abs(p["lat"] - 51.09) < 1e-3 and abs(p["lon"] - 71.42) < 1e-3]
            assert match, "синтетическая точка должна попасть в ответ"
            assert match[0]["days"] == 15
            # Ключей помимо lat/lon/days быть не должно (компактный ответ).
            assert set(match[0].keys()) == {"lat", "lon", "days"}
    finally:
        await execute("DELETE FROM outcome_labels WHERE listing_id = $1", lid)
        await execute("DELETE FROM apartment_listings WHERE id = $1", lid)


@pytest.mark.asyncio
async def test_liquidity_points_excludes_censored_active_listings(db):
    """Активное объявление (time_on_market IS NULL, censored) не должно
    попадать в ответ — иначе тепловая карта смешивала бы факт наблюдения
    с его отсутствием (Unknown != average)."""
    from bot.db.pg import execute
    suffix = uuid.uuid4().hex[:8]
    lid = f"__test_liq_active_{suffix}__"
    now = datetime.now(timezone.utc)
    try:
        await execute(
            """
            INSERT INTO apartment_listings (id, url, price, area, rooms, lat, lon,
                                             is_active, first_seen, last_seen)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            """,
            lid, f"https://krisha.kz/test/{lid}", 25_000_000, 45.0, 2,
            51.19, 71.52, True, now - timedelta(days=5), now,
        )
        # НЕТ строки outcome_labels — как раз обычный случай для активного.

        from bot.admin_web import create_admin_app
        from bot.db.compat import BotDB
        from httpx import AsyncClient, ASGITransport
        app = create_admin_app(BotDB("/tmp/__test_liq_admin2.db"), admin_password="x", bot_version="test")
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/admin/api/liquidity-points")
            data = r.json()
            match = [p for p in data["points"] if abs(p["lat"] - 51.19) < 1e-3 and abs(p["lon"] - 71.52) < 1e-3]
            assert not match, "активное (censored) объявление без time_on_market не должно попадать в ответ"
    finally:
        await execute("DELETE FROM apartment_listings WHERE id = $1", lid)
