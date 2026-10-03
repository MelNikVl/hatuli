"""Привязка объявлений к ЖК по id (задача 2026-10-03) — на реальной БД с
изолированными тестовыми строками (паттерн tests/test_listing_detail.py)."""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")
PFX = "__test_cxbind__"


@pytest_asyncio.fixture
async def db():
    from bot.db.pg import init_pool, close_pool, execute
    await init_pool(DATABASE_URL)
    await _cleanup(execute)
    yield execute
    await _cleanup(execute)
    await close_pool()


async def _cleanup(execute):
    await execute(f"DELETE FROM listing_complex_resolution_log WHERE listing_id LIKE '{PFX}%'")
    await execute(f"DELETE FROM apartment_listings WHERE id LIKE '{PFX}%'")
    await execute(f"DELETE FROM complexes WHERE name LIKE '{PFX}%'")


async def _cx(execute, name, lat, lon):
    from bot.db.pg import fetchval
    return await fetchval("INSERT INTO complexes (name, lat, lon) VALUES ($1, $2, $3) RETURNING id", name, lat, lon)


async def _listing(execute, lid, complex_name=None, lat=None, lon=None):
    await execute("""INSERT INTO apartment_listings (id, url, price, area, rooms, complex_name, lat, lon,
                     is_active, first_seen, last_seen) VALUES ($1, $1, 1, 50, 2, $2, $3, $4, TRUE, $5, $5)""",
                  lid, complex_name, lat, lon, datetime.now(timezone.utc))


async def _get(lid):
    from bot.db.pg import fetchrow
    return dict(await fetchrow("SELECT complex_id, complex_resolution FROM apartment_listings WHERE id=$1", lid))


@pytest.mark.asyncio
async def test_name_geo_and_ambiguity(db):
    from bot.core.complex_binding import resolve_complex_ids
    execute = db
    # Точка в пустом месте за городом, чтобы не задеть реальные дома
    lat, lon = 50.5, 70.5
    a = await _cx(execute, PFX + "A", lat, lon)
    await _cx(execute, PFX + "dup", lat + 0.1, lon)
    await _cx(execute, PFX + "DUP ", lat + 0.2, lon)   # то же имя после lower/trim
    await _listing(execute, PFX + "1", complex_name=PFX + "a ")          # имя (регистр/пробелы)
    await _listing(execute, PFX + "2", lat=lat + 0.0003, lon=lon)          # ~33 м, без имени → geo
    await _listing(execute, PFX + "3", complex_name=PFX + "dup")           # неоднозначное имя → NULL
    await _listing(execute, PFX + "4", complex_name="Чужой ЖК", lat=lat, lon=lon)  # имя есть, не совпало → без гео
    await _listing(execute, PFX + "5", lat=lat + 0.002, lon=lon)           # ~220 м → NULL
    await resolve_complex_ids(id_like=PFX + '%')
    assert await _get(PFX + "1") == {"complex_id": a, "complex_resolution": "name"}
    assert await _get(PFX + "2") == {"complex_id": a, "complex_resolution": "geo"}
    assert (await _get(PFX + "3"))["complex_id"] is None
    assert (await _get(PFX + "4"))["complex_id"] is None
    assert (await _get(PFX + "5"))["complex_id"] is None
    from bot.db.pg import fetchval
    n_log = await fetchval(f"SELECT count(*) FROM listing_complex_resolution_log WHERE listing_id LIKE '{PFX}%'")
    assert n_log == 2
    # идемпотентность: второй прогон ничего не меняет по тестовым строкам
    await resolve_complex_ids(id_like=PFX + '%')
    assert await fetchval(f"SELECT count(*) FROM listing_complex_resolution_log WHERE listing_id LIKE '{PFX}%'") == 2


@pytest.mark.asyncio
async def test_geo_ambiguous_when_two_houses_close(db):
    from bot.core.complex_binding import resolve_complex_ids
    execute = db
    lat, lon = 50.6, 70.6
    await _cx(execute, PFX + "B1", lat, lon)
    await _cx(execute, PFX + "B2", lat + 0.0001, lon)                     # ~11 м от первого
    await _listing(execute, PFX + "6", lat=lat + 0.00005, lon=lon)
    await resolve_complex_ids(id_like=PFX + '%')
    assert (await _get(PFX + "6"))["complex_id"] is None


def test_scope_sql_builds():
    from bot.core.complex_binding import build_sql
    assert "a.is_active IS NOT FALSE" in build_sql(True)
    assert "{scope}" not in build_sql(False)
    assert "a.id LIKE $1" in build_sql(False, "x%")
