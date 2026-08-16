"""Регрессия для задачи 2026-08-15 ("Location Reliability Phase", коммит
"двойные школы + building_age") — bot/score_layers/schools.py::compute()
с новым опциональным university_only. Дефолт False должен остаться
1:1 старым поведением для остальных потребителей модуля (per-listing
скоринг, compute_all_layers) — university_only=True используется ТОЛЬКО
из bot/core/location_score.py.

Реальная БД (city_poi, 1398 реальных строк) — синтетические точки в
районе Алматы (REF_LAT/REF_LON), тот же приём, что в tests/test_
location_score_schools_kindergartens.py — заведомо далеко от реальных
POI Астаны, гарантирует что найдутся именно наши синтетические записи."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

REF_LAT = 43.2500
REF_LON = 76.9500


@pytest_asyncio.fixture
async def db():
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    yield
    await close_pool()


async def _insert_poi(name, kind, lat=REF_LAT, lon=REF_LON):
    from bot.db.pg import fetchval
    return await fetchval(
        "INSERT INTO city_poi (kind, name, lat, lon) VALUES ($1, $2, $3, $4) RETURNING id",
        kind, name, lat, lon)


async def _cleanup(*ids):
    from bot.db.pg import execute
    await execute("DELETE FROM city_poi WHERE id = ANY($1::int[])", list(ids))


@pytest.mark.asyncio
async def test_university_only_ignores_school_and_kindergarten(db):
    from bot.score_layers.schools import compute
    ids = [
        await _insert_poi("__test_sl_school__", "school"),
        await _insert_poi("__test_sl_kg__", "kindergarten"),
    ]
    try:
        adj, reason = await compute({"lat": REF_LAT, "lon": REF_LON}, university_only=True)
        assert adj == 0
        assert "вуз" in reason
    finally:
        await _cleanup(*ids)


@pytest.mark.asyncio
async def test_university_only_still_counts_university(db):
    from bot.score_layers.schools import compute
    ids = [
        await _insert_poi("__test_sl_school2__", "school"),
        await _insert_poi("__test_sl_uni__", "university"),
    ]
    try:
        adj, reason = await compute({"lat": REF_LAT, "lon": REF_LON}, university_only=True)
        assert adj == 2
        assert "вуз" in reason
    finally:
        await _cleanup(*ids)


@pytest.mark.asyncio
async def test_default_behavior_unchanged_school_and_kindergarten(db):
    """Дефолт university_only=False — старое поведение 1:1 (важно для
    остальных потребителей модуля, не только location_score.py)."""
    from bot.score_layers.schools import compute
    ids = [
        await _insert_poi("__test_sl_school3__", "school"),
        await _insert_poi("__test_sl_kg3__", "kindergarten"),
    ]
    try:
        adj, reason = await compute({"lat": REF_LAT, "lon": REF_LON})
        assert adj == 5
        assert "садик" in reason
    finally:
        await _cleanup(*ids)


@pytest.mark.asyncio
async def test_university_only_no_poi_nearby(db, monkeypatch):
    """Найдено при разборе падения CI PR #1 (задача 2026-08-16, "Исправь
    падение CI"): на CI-раннере city_poi пустая (свежая БД без сид-данных)
    -> compute() уходит в фолбэк bot.score_layers.osm.overpass_cached,
    а реальные зеркала Overpass недетерминированно отвечают 504/406/
    timeout -> тест ловил ветку "OSM недоступен", а не сценарий "POI
    рядом нет", который должен проверять. Мокаем overpass_cached
    фиксированным пустым ответом — тест детерминирован независимо от
    сети И от того, наполнена ли city_poi в конкретном окружении (если
    наполнена — сработает более ранняя ветка _from_local_table, тоже
    "вузов не найдено", ассерт тот же). Тест реального Overpress —
    test_university_only_no_poi_nearby_live_overpass ниже, live_data."""
    from bot.score_layers import schools

    async def _fake_overpass_cached(lat, lon, key, query):
        return {"elements": []}

    monkeypatch.setattr(schools, "overpass_cached", _fake_overpass_cached)

    # Точка без синтетических соседей — далеко и от реальных POI Астаны,
    # и от других тестовых точек этого файла.
    adj, reason = await schools.compute({"lat": REF_LAT + 0.5, "lon": REF_LON + 0.5}, university_only=True)
    assert adj == 0
    assert "вуз" in reason
    # "OSM недоступен" — сигнал сетевого сбоя, не "POI не найдено" — не
    # должен проходить как допустимый результат этого теста (иначе тест
    # снова маскирует падение сети вместо проверки логики).
    assert "недоступен" not in reason.lower()


@pytest.mark.asyncio
@pytest.mark.live_data
async def test_university_only_no_poi_nearby_live_overpass(db):
    """Тот же сценарий, но без мока — реальный поход в Overpass (сеть,
    внешний сервис). Не гоняется в CI (см. pytest.ini live_data и
    .github/workflows/ci.yml — `-m "not live_data"`): зеркала Overpass
    отвечают недетерминированно (504/406/timeout), падение здесь не
    значит регрессию в коде. Локально: venv/bin/pytest -m live_data -v."""
    from bot.score_layers.schools import compute
    adj, reason = await compute({"lat": REF_LAT + 0.5, "lon": REF_LON + 0.5}, university_only=True)
    assert adj == 0
    assert "нет" in reason.lower() or "не найдено" in reason.lower() or "вуз" in reason
