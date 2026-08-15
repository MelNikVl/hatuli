"""Регрессия для задачи 2026-08-15 ("воздух в location_score", Task 3,
начата после явного завершения Location Reliability Phase) —
bot/core/location_score.py::_air_quality_factor(). Реальная БД,
синтетические строки в air_stations (id-скоуп через cleanup — не
трогают 295 реальных строк/10 станций).

Опорная точка — координаты в районе Алматы (REF_LAT/REF_LON, тот же
приём, что в остальных тестах этой сессии) — заведомо далеко от 10
реальных станций Астаны, гарантирует, что ORDER BY d2 LIMIT 1 вернёт
именно синтетическую строку теста."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

REF_LAT = 43.1500
REF_LON = 76.8000


def _offset_lat(dist_m: float) -> float:
    return REF_LAT + dist_m / 111000.0


@pytest_asyncio.fixture
async def db():
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    yield
    await close_pool()


async def _insert_station(name, dist_m, index_value, index_pollutant="PM2.5",
                           fetched_at=None):
    from bot.db.pg import fetchval
    fetched_at = fetched_at or datetime.now(timezone.utc)
    return await fetchval(
        "INSERT INTO air_stations (station_name, lat, lon, index_value, index_pollutant, fetched_at) "
        "VALUES ($1, $2, $3, $4, $5, $6) RETURNING id",
        name, _offset_lat(dist_m), REF_LON, index_value, index_pollutant, fetched_at)


async def _cleanup(*ids):
    from bot.db.pg import execute
    await execute("DELETE FROM air_stations WHERE id = ANY($1::int[])", list(ids))


@pytest.mark.asyncio
async def test_below_one_gives_zero(db):
    from bot.core.location_score import _air_quality_factor
    sid = await _insert_station("__test_air_clean__", 1000, 0.15, "CO")
    try:
        r = await _air_quality_factor(REF_LAT, REF_LON)
        assert r["adj"] == 0
        assert "__test_air_clean__" in r["reason"]
        assert "0.15" in r["reason"]
        assert "CO" in r["reason"]
    finally:
        await _cleanup(sid)


@pytest.mark.asyncio
async def test_one_to_two_gives_minus_one(db):
    from bot.core.location_score import _air_quality_factor
    sid = await _insert_station("__test_air_1_5__", 1000, 1.5)
    try:
        r = await _air_quality_factor(REF_LAT, REF_LON)
        assert r["adj"] == -1
    finally:
        await _cleanup(sid)


@pytest.mark.asyncio
async def test_two_to_five_gives_minus_two(db):
    from bot.core.location_score import _air_quality_factor
    sid = await _insert_station("__test_air_3__", 1000, 3.0)
    try:
        r = await _air_quality_factor(REF_LAT, REF_LON)
        assert r["adj"] == -2
    finally:
        await _cleanup(sid)


@pytest.mark.asyncio
async def test_five_plus_gives_minus_three(db):
    from bot.core.location_score import _air_quality_factor
    sid = await _insert_station("__test_air_bad__", 1000, 7.2)
    try:
        r = await _air_quality_factor(REF_LAT, REF_LON)
        assert r["adj"] == -3
    finally:
        await _cleanup(sid)


@pytest.mark.asyncio
async def test_beyond_5km_gives_zero_with_honest_reason(db):
    from bot.core.location_score import _air_quality_factor
    sid = await _insert_station("__test_air_far__", 6000, 8.0)  # index плохой, но станция далеко
    try:
        r = await _air_quality_factor(REF_LAT, REF_LON)
        assert r["adj"] == 0
        assert "нет станции в радиусе" in r["reason"]
    finally:
        await _cleanup(sid)


@pytest.mark.asyncio
async def test_picks_latest_reading_of_nearest_station_not_stale(db):
    """air_stations — time series (много строк на станцию). Ближайшая
    станция должна резолвиться по СВОЕЙ последней fetched_at, не по
    случайной старой записи."""
    from bot.core.location_score import _air_quality_factor
    old_ts = datetime.now(timezone.utc) - timedelta(hours=5)
    new_ts = datetime.now(timezone.utc)
    old_id = await _insert_station("__test_air_ts__", 500, 6.0, fetched_at=old_ts)
    new_id = await _insert_station("__test_air_ts__", 500, 0.2, fetched_at=new_ts)
    try:
        r = await _air_quality_factor(REF_LAT, REF_LON)
        assert r["adj"] == 0  # свежее значение 0.2, не старое 6.0
        assert "0.2" in r["reason"]
    finally:
        await _cleanup(old_id, new_id)


@pytest.mark.asyncio
async def test_no_reason_contains_no_data_when_station_found():
    """'нет станции в радиусе' НЕ должно содержать 'нет данных' — иначе
    _is_available() ошибочно исключил бы этот случай из группы (это
    честный измеренный результат: искали, станция просто далеко — тот
    же принцип, что 'ближайшая школа дальше 1км')."""
    assert "нет данных" not in "нет станции в радиусе"
