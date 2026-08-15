"""Регрессия для задачи 2026-08-15 ("Location Reliability Phase", коммит
"двойные школы + building_age") — bot/core/location_score.py::
compute_complex_location_score() должна звать OSM-слой schools с
university_only=True, когда точные astana_schools/kindergartens факторы
реально посчитаны (обычный случай), и university_only=False только в
редком fallback (оба факторов вернули "нет данных").

Остальные OSM-слои (noise/transit/amenities/parks) и poi-прогрев
подменены фейками — тест не бьёт в реальный Overpass (тот же приём, что
tests/test_complex_location_detail.py)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

REF_LAT = 43.3000
REF_LON = 77.0000


@pytest_asyncio.fixture
async def db():
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    yield
    await close_pool()


def _mock_osm_layers(monkeypatch):
    """noise/transit/amenities/parks -> нейтральный фейк; schools ->
    захватывает university_only, которым его позвали; poi.fetch_poi ->
    пустой список (прогрев кэша не бьёт в сеть)."""
    import bot.score_layers.noise as noise_module
    import bot.score_layers.transit as transit_module
    import bot.score_layers.amenities as amenities_module
    import bot.score_layers.parks as parks_module
    import bot.score_layers.schools as schools_module
    import bot.score_layers.poi as poi_module

    captured = {}

    async def _fake_simple(listing):
        return 0, "фейк"

    async def _fake_schools_compute(listing, university_only=False):
        captured["university_only"] = university_only
        return 0, "фейк-школы"

    async def _fake_fetch_poi(lat, lon):
        return []

    monkeypatch.setattr(noise_module, "compute", _fake_simple)
    monkeypatch.setattr(transit_module, "compute", _fake_simple)
    monkeypatch.setattr(amenities_module, "compute", _fake_simple)
    monkeypatch.setattr(parks_module, "compute", _fake_simple)
    monkeypatch.setattr(schools_module, "compute", _fake_schools_compute)
    monkeypatch.setattr(poi_module, "fetch_poi", _fake_fetch_poi)
    return captured


async def _insert_school(name, lat, lon):
    from bot.db.pg import fetchval
    return await fetchval(
        "INSERT INTO astana_schools (name, lat, lon, type) VALUES ($1, $2, $3, 'общеобразовательная') RETURNING id",
        name, lat, lon)


async def _cleanup_schools(*ids):
    from bot.db.pg import execute
    await execute("DELETE FROM astana_schools WHERE id = ANY($1::int[])", list(ids))


@pytest.mark.asyncio
async def test_precise_data_available_calls_osm_schools_university_only(db, monkeypatch):
    from bot.core.location_score import compute_complex_location_score

    captured = _mock_osm_layers(monkeypatch)
    # astana_schools непустая городская таблица (160 реальных строк) —
    # _schools_factor() ВСЕГДА находит ближайшую (нет "нет данных" в
    # обычном режиме), даже без синтетической вставки. Вставляем свою
    # только чтобы тест не зависел от возможного будущего опустошения
    # реальной таблицы.
    sid = await _insert_school("__test_ldc_school__", REF_LAT, REF_LON)
    try:
        result = await compute_complex_location_score(REF_LAT, REF_LON)
        assert result is not None
        assert captured["university_only"] is True
    finally:
        await _cleanup_schools(sid)


@pytest.mark.asyncio
async def test_astana_data_unavailable_falls_back_to_full_osm_schools(db, monkeypatch):
    """Редкий fallback: оба точных фактора вернули "нет данных" (реальный
    сбой БД для этих двух конкретных запросов, смоделирован через
    monkeypatch fetchrow) -> OSM-слой schools звать в полном режиме."""
    import bot.db.pg as pg_module

    async def _fake_fetchrow(*a, **kw):
        return None

    monkeypatch.setattr(pg_module, "fetchrow", _fake_fetchrow)
    captured = _mock_osm_layers(monkeypatch)

    from bot.core.location_score import compute_complex_location_score
    result = await compute_complex_location_score(REF_LAT, REF_LON)
    assert result is not None
    assert captured["university_only"] is False
