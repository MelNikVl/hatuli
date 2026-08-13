"""Регрессия save_to_db() — живой баг 2026-08-13: полный backfill-прогон
(1241 ЖК) записал 0 — UPDATE падал на КАЖДОЙ строке
("could not determine data type of parameter $5", PostgreSQL не мог
вывести тип $5/$6 без явного каста — первое упоминание внутри
"$5 IS NOT NULL" в CASE, тип не required). Юнит-тест на
parse_deadlines_by_queue()/parse_complex_page() эту ошибку НЕ ловит —
это чисто SQL-уровня баг, нужен реальный execute() против БД, не
проверка Python-логики (тот же урок, что smoke-тест на роут: код
может быть "логически верным" и всё равно падать на исполнении)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio
from dotenv import load_dotenv

load_dotenv()

from krisha_complex_import import save_to_db, parse_deadlines_by_queue

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")


@pytest_asyncio.fixture
async def db():
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    yield
    await close_pool()


@pytest_asyncio.fixture
async def test_complex(db):
    from bot.db.pg import fetchval, execute
    cid = await fetchval(
        "INSERT INTO complexes (name, source_info) VALUES ('__test_kci_complex__', NULL) RETURNING id")
    try:
        yield cid
    finally:
        await execute("DELETE FROM complexes WHERE id = $1", cid)


@pytest.mark.asyncio
async def test_save_to_db_with_full_data_no_sql_error(test_complex):
    """Живой сценарий бага: все поля заполнены (developer/address/
    year_built/lat/lon/photo_url/url) — именно так падал реальный
    прогон на 1241 ЖК."""
    data = {
        test_complex: {
            "developer": "ТОО Тест Девелопмент", "status": "Строящийся",
            "deadline": "IV квартал 2026 г.", "deadlines_by_queue": None,
            "address": "Астана, тестовая улица, 1", "rating": 4.5, "reviews_cnt": 3,
            "url": "https://krisha.kz/complex/show/astana/test/",
            "lat": 51.1, "lon": 71.4, "photo_url": "https://example.com/photo.jpg",
            "name": "__test_kci_complex__",
        }
    }
    saved = await save_to_db(data)
    assert saved == 1

    from bot.db.pg import fetchrow
    row = await fetchrow("SELECT address, lat, lon, developer_id FROM complexes WHERE id=$1", test_complex)
    assert row["address"] == "Астана, тестовая улица, 1"
    assert row["lat"] == pytest.approx(51.1)
    assert row["lon"] == pytest.approx(71.4)
    assert row["developer_id"] is not None


@pytest.mark.asyncio
async def test_save_to_db_with_no_coords_no_sql_error(test_complex):
    """lat/lon отсутствуют (None) — именно этот случай был первым
    репро бага (AmbiguousParameterError и на None, и на реальном float)."""
    data = {
        test_complex: {
            "developer": None, "status": None, "deadline": None, "deadlines_by_queue": None,
            "address": None, "rating": None, "reviews_cnt": None, "url": None,
            "lat": None, "lon": None, "photo_url": None, "name": "__test_kci_complex__",
        }
    }
    saved = await save_to_db(data)
    assert saved == 1


def test_parse_deadlines_by_queue_smoke():
    result = parse_deadlines_by_queue(
        "Первая очередь - IV квартал 2020 г. Вторая очередь - II квартал 2022 г.")
    assert len(result) == 2
    assert result[0]["label"] == "Первая очередь"
