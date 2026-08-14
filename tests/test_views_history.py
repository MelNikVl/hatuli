"""Регрессия для задачи 2026-08-14 (Г2, docs/data_collection_audit.md):
service_viewcount.py должен писать КАЖДОЕ успешное наблюдение в
views_history (append-only), не только обновлять текущий снимок
apartment_listings.views_count.

_record_observation() тестируется напрямую (не через run_cycle()) —
run_cycle() берёт реальную очередь активных объявлений батчем; гонять
его в тестах означало бы дёргать views_count_updated_at случайных
боевых строк в общей БД. Отдельный HTTP-подобный тест на run_cycle()
ниже монки-патчит fetch_view_count и САМ батч-запрос, чтобы остаться
изолированным от боевых данных."""
import os
import sys

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


@pytest_asyncio.fixture
async def one_listing(db):
    from bot.db.pg import execute
    await execute("""
        INSERT INTO apartment_listings (id, url, is_active)
        VALUES ('__test_vh_1__', 'https://krisha.kz/a/show/1', TRUE)
    """)
    try:
        yield "__test_vh_1__"
    finally:
        await execute("DELETE FROM views_history WHERE listing_id = '__test_vh_1__'")
        await execute("DELETE FROM apartment_listings WHERE id = '__test_vh_1__'")


@pytest.mark.asyncio
async def test_record_observation_appends_history_and_updates_snapshot(one_listing):
    from service_viewcount import _record_observation
    from bot.db.pg import execute, fetch

    await _record_observation(execute, one_listing, 42)

    snap = await fetch("SELECT views_count FROM apartment_listings WHERE id=$1", one_listing)
    assert snap[0]["views_count"] == 42

    hist = await fetch(
        "SELECT views_count, observed_at FROM views_history WHERE listing_id=$1", one_listing)
    assert len(hist) == 1
    assert hist[0]["views_count"] == 42
    assert hist[0]["observed_at"] is not None


@pytest.mark.asyncio
async def test_record_observation_appends_even_when_value_unchanged(one_listing):
    # Ключевое отличие от price_history: "не изменилось между X и Y" —
    # тоже нужная точка на графике динамики, не только смена значения.
    from service_viewcount import _record_observation
    from bot.db.pg import execute, fetch

    await _record_observation(execute, one_listing, 10)
    await _record_observation(execute, one_listing, 10)

    hist = await fetch(
        "SELECT views_count FROM views_history WHERE listing_id=$1 ORDER BY observed_at", one_listing)
    assert len(hist) == 2
    assert [h["views_count"] for h in hist] == [10, 10]


@pytest.mark.asyncio
async def test_run_cycle_uses_record_observation_on_success(one_listing, monkeypatch):
    """Смоук на run_cycle() целиком — батч-запрос подменён на один тестовый
    listing_id, чтобы не задевать боевую очередь."""
    import service_viewcount
    from bot.db.pg import fetch

    async def _fake_pg_fetch(sql, *args):
        return await fetch(
            "SELECT id, url FROM apartment_listings WHERE id = $1", one_listing)

    async def _fake_fetch_view_count(browser, url):
        return 99

    monkeypatch.setattr(service_viewcount, "fetch_view_count", _fake_fetch_view_count)
    import bot.db.pg as pg_module
    monkeypatch.setattr(pg_module, "fetch", _fake_pg_fetch)

    result = await service_viewcount.run_cycle(browser=None)
    assert result == {"attempted": 1, "updated": 1}

    hist = await fetch("SELECT views_count FROM views_history WHERE listing_id=$1", one_listing)
    assert len(hist) == 1 and hist[0]["views_count"] == 99
