"""Регрессия для бага, найденного при аудите незакоммиченной работы
DeepSeek (задача 2026-08-15): /admin/developer-reviews и
/admin/developer-reviews/update использовали psycopg2-style %s-
плейсхолдеры через asyncpg-обёртки pg_fetch/pg_exec (bot/db/pg.py) —
asyncpg понимает только $1/$2, оба роута падали PostgresSyntaxError.
Тот же ASGI-клиент, что tests/test_kzk_registry_admin.py. Реальная БД,
синтетические записи (id-скоуп)."""
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
async def db():
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    yield
    await close_pool()


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


async def _insert_review(review_text="__test_dr_review__", sentiment="negative"):
    from bot.db.pg import fetchval
    return await fetchval(
        "INSERT INTO developer_reviews (source, review_text, sentiment) "
        "VALUES ('2gis', $1, $2) RETURNING id",
        review_text, sentiment)


async def _cleanup(*ids):
    from bot.db.pg import execute
    await execute("DELETE FROM developer_reviews WHERE id = ANY($1::int[])", list(ids))


@pytest.mark.asyncio
async def test_developer_reviews_page_renders_without_sentiment_filter(client, db):
    """Без ?sentiment= — не задевает сломанный $1-запрос напрямую, но
    падал бы тем же образом, если бы регрессия зацепила соседний запрос
    заодно — проверяем оба пути страницы."""
    rid = await _insert_review()
    try:
        r = await client.get("/admin/developer-reviews")
        assert r.status_code == 200
        assert "Отзывы" in r.text
    finally:
        await _cleanup(rid)


@pytest.mark.asyncio
async def test_developer_reviews_page_with_sentiment_filter_no_syntax_error(client, db):
    """Голая регрессия: ?sentiment=negative бил ИМЕННО в %s-плейсхолдер
    (WHERE dr.sentiment = %s) -> PostgresSyntaxError -> 500 до фикса."""
    rid = await _insert_review(sentiment="negative")
    try:
        r = await client.get("/admin/developer-reviews?sentiment=negative")
        assert r.status_code == 200
        assert "__test_dr_review__" in r.text
    finally:
        await _cleanup(rid)


@pytest.mark.asyncio
async def test_developer_reviews_update_no_syntax_error(client, db):
    """UPDATE ... SET sentiment = %s WHERE id = %s -> PostgresSyntaxError
    до фикса. После фикса — реально обновляет строку, не молча глотает
    ошибку (роут не проверяет статус запроса, поэтому падение БД раньше
    было незаметно снаружи — 302 в обоих случаях; проверяем эффект в БД,
    не только код ответа)."""
    from bot.db.pg import fetchrow
    rid = await _insert_review(sentiment="neutral")
    try:
        r = await client.post("/admin/developer-reviews/update",
                               data={"id": str(rid), "sentiment": "positive"})
        assert r.status_code in (200, 302)
        row = await fetchrow("SELECT sentiment FROM developer_reviews WHERE id=$1", rid)
        assert row["sentiment"] == "positive"
    finally:
        await _cleanup(rid)
