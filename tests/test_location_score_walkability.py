"""Регрессия для Фазы L3 walkability (задача 2026-08-15, миграция 075) —
walking-ветка bot/core/location_score.py::_schools_factor()/
_kindergartens_factor(): при complex_id расстояние берётся из свежей
строки complex_walkability (OSRM, маршрут пешком), не из SQL-аппроксимации.

Реальная БД, синтетические строки (name-префикс __test_, cleanup в
finally) — тот же паттерн, что tests/test_location_score_schools_
kindergartens.py. Опорная точка — район Алматы (REF_LAT/REF_LON), в
сотнях км от реальных школ Астаны: координаты ЖК/назначения синтетические,
walking-ветке безразлично, что рядом нет реальных POI (она читает
complex_walkability по complex_id, не по координатам)."""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

REF_LAT = 43.2220
REF_LON = 76.8512


@pytest_asyncio.fixture
async def db():
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    yield
    await close_pool()


async def _insert_complex(name="__test_cw_factor__"):
    from bot.db.pg import fetchval
    return await fetchval("INSERT INTO complexes (name) VALUES ($1) RETURNING id", name)


async def _insert_walkability(cid, dtype, walking, haversine, barrier=None,
                              dest_lat=None, dest_lon=None, age_days=0,
                              no_route_reason=None):
    from bot.db.pg import execute
    ts = datetime.now(timezone.utc) - timedelta(days=age_days)
    await execute("""
        INSERT INTO complex_walkability (
            complex_id, destination_type, computed_at, walking_distance_m,
            haversine_distance_m, barrier, dest_lat, dest_lon,
            no_route_reason, engine_version
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'osrm-foot-v1@test')
    """, cid, dtype, ts, walking, haversine, barrier, dest_lat, dest_lon,
        no_route_reason)


async def _insert_school(name, lat, lon, school_type):
    from bot.db.pg import fetchval
    return await fetchval(
        "INSERT INTO astana_schools (name, lat, lon, type) VALUES ($1, $2, $3, $4) RETURNING id",
        name, lat, lon, school_type)


async def _cleanup(cid=None, school_ids=()):
    from bot.db.pg import execute
    if cid is not None:
        await execute("DELETE FROM complexes WHERE id=$1", cid)  # cascade -> complex_walkability
    if school_ids:
        await execute("DELETE FROM astana_schools WHERE id = ANY($1::int[])", list(school_ids))


@pytest.mark.asyncio
async def test_schools_walking_distance_used_when_complex_id(db):
    """Свежая walking-строка: dist берётся из неё (450м пешком → база 2),
    тип/рейтинг — из школы по координатам НАЗНАЧЕНИЯ (лицей → +1), reason
    помечен 'пешком'."""
    from bot.core.location_score import _schools_factor
    cid = await _insert_complex()
    sid = await _insert_school("__test_cw_lyceum__", REF_LAT + 0.001, REF_LON, "лицей")
    try:
        await _insert_walkability(cid, "school", walking=450.0, haversine=300.0,
                                  barrier=False, dest_lat=REF_LAT + 0.001, dest_lon=REF_LON)
        r = await _schools_factor(REF_LAT, REF_LON, complex_id=cid)
        assert r["adj"] == 3  # 300-500м = 2, +1 бонус (лицей)
        assert "пешком" in r["reason"]
        assert "лицей" in r["reason"]
    finally:
        await _cleanup(cid, (sid,))


@pytest.mark.asyncio
async def test_schools_walking_barrier_note_in_reason(db):
    from bot.core.location_score import _schools_factor
    cid = await _insert_complex()
    try:
        await _insert_walkability(cid, "school", walking=812.0, haversine=205.0,
                                  barrier=True, dest_lat=REF_LAT, dest_lon=REF_LON)
        r = await _schools_factor(REF_LAT, REF_LON, complex_id=cid)
        assert r["adj"] == 1  # 500-1000м пешком
        assert "⚠️" in r["reason"]
        assert "205" in r["reason"]  # хаверсин показан для контраста
    finally:
        await _cleanup(cid)


@pytest.mark.asyncio
async def test_schools_no_route_falls_back_to_row_haversine(db):
    """walking=NULL (маршрут не построен) — берём haversine ИЗ ТОЙ ЖЕ
    строки с честной пометкой, не притворяемся, что маршрут есть."""
    from bot.core.location_score import _schools_factor
    cid = await _insert_complex()
    try:
        await _insert_walkability(cid, "school", walking=None, haversine=280.0,
                                  no_route_reason="no_snap",
                                  dest_lat=REF_LAT, dest_lon=REF_LON)
        r = await _schools_factor(REF_LAT, REF_LON, complex_id=cid)
        assert r["adj"] == 3  # 280м <= 300
        assert "маршрут не построен" in r["reason"]
        assert "пешком" not in r["reason"]
    finally:
        await _cleanup(cid)


@pytest.mark.asyncio
async def test_schools_stale_walkability_row_ignored(db):
    """Строка старше 45 дней — игнорируется, фолбэк на SQL-аппроксимацию
    (в районе Алматы реальных школ нет → 'ближайшая школа дальше 1км')."""
    from bot.core.location_score import _schools_factor
    cid = await _insert_complex()
    try:
        await _insert_walkability(cid, "school", walking=100.0, haversine=90.0,
                                  age_days=60)
        r = await _schools_factor(REF_LAT, REF_LON, complex_id=cid)
        assert "пешком" not in r["reason"]
        assert r["adj"] == 0  # ближайшая реальная школа — в Астане, ~850км
    finally:
        await _cleanup(cid)


@pytest.mark.asyncio
async def test_schools_no_complex_id_keeps_straight_line(db):
    """Вызов по голым координатам (complex_id=None) — прежний путь,
    complex_walkability не читается вообще."""
    from bot.core.location_score import _schools_factor
    r = await _schools_factor(REF_LAT, REF_LON)
    assert "пешком" not in r["reason"]


@pytest.mark.asyncio
async def test_kindergartens_walking_distance_used(db):
    from bot.core.location_score import _kindergartens_factor
    cid = await _insert_complex()
    try:
        await _insert_walkability(cid, "kindergarten", walking=250.0, haversine=200.0,
                                  barrier=False)
        r = await _kindergartens_factor(REF_LAT, REF_LON, complex_id=cid)
        assert r["adj"] == 2
        assert "пешком" in r["reason"]
    finally:
        await _cleanup(cid)


@pytest.mark.asyncio
async def test_kindergartens_walking_barrier_downgrades_vs_straight(db):
    """Ключевой смысл walkability: 480м ПЕШКОМ (adj 1), хотя по прямой
    190м (было бы adj 2) — барьер честно понижает оценку."""
    from bot.core.location_score import _kindergartens_factor
    cid = await _insert_complex()
    try:
        await _insert_walkability(cid, "kindergarten", walking=480.0, haversine=190.0,
                                  barrier=True)
        r = await _kindergartens_factor(REF_LAT, REF_LON, complex_id=cid)
        assert r["adj"] == 1
        assert "⚠️" in r["reason"]
    finally:
        await _cleanup(cid)
