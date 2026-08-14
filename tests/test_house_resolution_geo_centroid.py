"""Регрессия для Фазы B, п.5 вердикт-стратегии (docs/verdict_strategy.md,
задача 2026-08-14): bot/core/house_resolution.resolve_complex_geo_centroid() —
центроид координат ЖК/дома, вынесенный из двух буквально дублировавшихся
SQL-запросов в terminal_extras.py (карточка ЖК + /admin/api/complex/{id}/
location-score). Оба вызывающих места уже покрыты end-to-end в
tests/test_house_resolution_geo.py — этот файл тестирует саму функцию
напрямую, включая случай без координат вовсе."""
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


@pytest.mark.asyncio
async def test_centroid_by_name_match(db):
    from bot.db.pg import fetchval, execute
    from bot.core.house_resolution import resolve_complex_geo_centroid
    cid = await fetchval("INSERT INTO complexes (name) VALUES ('__test_geo_by_name__') RETURNING id")
    lid = "__test_geo_listing_name__"
    try:
        await execute(
            "INSERT INTO apartment_listings (id, complex_name, lat, lon, price, area, rooms) "
            "VALUES ($1, '__test_geo_by_name__', 51.20, 71.40, 30000000, 60.0, 2)", lid)
        centroid = await resolve_complex_geo_centroid(cid, "__test_geo_by_name__")
        assert centroid is not None
        assert round(centroid[0], 2) == 51.20
        assert round(centroid[1], 2) == 71.40
    finally:
        await execute("DELETE FROM apartment_listings WHERE id = $1", lid)
        await execute("DELETE FROM complexes WHERE id = $1", cid)


@pytest.mark.asyncio
async def test_centroid_by_resolved_house_id_when_name_differs(db):
    """Объявление всё ещё называет ЗОНТИКА в тексте, но resolved_house_id
    уже указывает на дом — центроид всё равно находится (см. докстринг
    функции), а не молча по чужим координатам зонтика/не находится вовсе."""
    from bot.db.pg import fetchval, execute
    from bot.core.house_resolution import resolve_complex_geo_centroid
    umbrella_id = await fetchval("INSERT INTO complexes (name, is_umbrella) VALUES ('__test_geo_umb__', TRUE) RETURNING id")
    house_id = await fetchval(
        "INSERT INTO complexes (name, parent_complex_id) VALUES ('__test_geo_house__', $1) RETURNING id",
        umbrella_id)
    lid = "__test_geo_listing_house__"
    try:
        await execute(
            "INSERT INTO apartment_listings (id, complex_name, lat, lon, price, area, rooms, resolved_house_id) "
            "VALUES ($1, '__test_geo_umb__', 51.30, 71.50, 30000000, 60.0, 2, $2)", lid, house_id)
        centroid = await resolve_complex_geo_centroid(house_id, "__test_geo_house__")
        assert centroid is not None
        assert round(centroid[0], 2) == 51.30
        assert round(centroid[1], 2) == 71.50
    finally:
        await execute("DELETE FROM apartment_listings WHERE id = $1", lid)
        await execute("DELETE FROM complexes WHERE id IN ($1, $2)", umbrella_id, house_id)


@pytest.mark.asyncio
async def test_centroid_none_when_no_listings_have_coords(db):
    from bot.db.pg import fetchval, execute
    from bot.core.house_resolution import resolve_complex_geo_centroid
    cid = await fetchval("INSERT INTO complexes (name) VALUES ('__test_geo_empty__') RETURNING id")
    try:
        centroid = await resolve_complex_geo_centroid(cid, "__test_geo_empty__")
        assert centroid is None
    finally:
        await execute("DELETE FROM complexes WHERE id = $1", cid)
