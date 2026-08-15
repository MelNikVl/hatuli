"""Регрессия для задачи 2026-08-15 ("Школы/садики в location_score") —
коммит 1: bot/core/location_score.py::_schools_factor()/_kindergartens_
factor() как отдельные функции (интеграция в compute_complex_location_
score()/_GROUPS["infra"] — коммит 2, не здесь).

Реальная БД, синтетические строки в astana_schools/astana_kindergartens
(id-скоуп через cleanup в finally, name-префикс __test_ — не трогают
160/131 реальных строк). Опорная точка — координаты в районе Алматы
(REF_LAT/REF_LON), заведомо в сотнях км от реальных школ/садиков Астаны
(все 291 реальных строки физически кластеризованы вокруг Астаны) —
гарантирует, что ORDER BY d2 LIMIT 1 в запросе всегда вернёт именно
синтетическую строку теста, а не случайно более близкую реальную.

Расстояния offset считаются той же формулой, что и в самой функции
(111.0 км/град широты, без сдвига по долготе — offset только по lat),
чтобы тестовая дистанция совпадала с тем, что вычислит SQL-запрос
_schools_factor()/_kindergartens_factor() без независимого округления."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

REF_LAT = 43.2220
REF_LON = 76.8512


def _offset_lat(dist_m: float) -> float:
    return REF_LAT + dist_m / 111000.0


@pytest_asyncio.fixture
async def db():
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    yield
    await close_pool()


async def _insert_school(name, dist_m, school_type):
    from bot.db.pg import fetchval
    return await fetchval(
        "INSERT INTO astana_schools (name, lat, lon, type) VALUES ($1, $2, $3, $4) RETURNING id",
        name, _offset_lat(dist_m), REF_LON, school_type)


async def _insert_kindergarten(name, dist_m):
    from bot.db.pg import fetchval
    return await fetchval(
        "INSERT INTO astana_kindergartens (name, lat, lon) VALUES ($1, $2, $3) RETURNING id",
        name, _offset_lat(dist_m), REF_LON)


async def _cleanup_schools(*ids):
    from bot.db.pg import execute
    await execute("DELETE FROM astana_schools WHERE id = ANY($1::int[])", list(ids))


async def _cleanup_kindergartens(*ids):
    from bot.db.pg import execute
    await execute("DELETE FROM astana_kindergartens WHERE id = ANY($1::int[])", list(ids))


# ── _schools_factor ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_schools_close_bonus_type_gets_type_bonus(db):
    from bot.core.location_score import _schools_factor
    sid = await _insert_school("__test_school_close_lyceum__", 200, "лицей")
    try:
        r = await _schools_factor(REF_LAT, REF_LON)
        assert r["adj"] == 4  # база 3 (<=300м) + 1 бонус (лицей)
        assert "лицей" in r["reason"]
    finally:
        await _cleanup_schools(sid)


@pytest.mark.asyncio
async def test_schools_mid_distance_no_bonus_type(db):
    from bot.core.location_score import _schools_factor
    sid = await _insert_school("__test_school_mid_regular__", 400, "общеобразовательная")
    try:
        r = await _schools_factor(REF_LAT, REF_LON)
        assert r["adj"] == 2  # 300-500м, без бонуса
    finally:
        await _cleanup_schools(sid)


@pytest.mark.asyncio
async def test_schools_far_distance_1000m_band(db):
    from bot.core.location_score import _schools_factor
    sid = await _insert_school("__test_school_700__", 700, "гимназия")
    try:
        r = await _schools_factor(REF_LAT, REF_LON)
        assert r["adj"] == 2  # 500-1000м = база 1 + бонус (гимназия) = 2
    finally:
        await _cleanup_schools(sid)


@pytest.mark.asyncio
async def test_schools_beyond_1km_zero_adj_no_bonus_applied(db):
    from bot.core.location_score import _schools_factor
    sid = await _insert_school("__test_school_far_lyceum__", 1500, "лицей")
    try:
        r = await _schools_factor(REF_LAT, REF_LON)
        assert r["adj"] == 0  # тип-бонус НЕ применяется за пределами 1км
        assert "лицей" not in r["reason"]
    finally:
        await _cleanup_schools(sid)


@pytest.mark.asyncio
async def test_schools_far_reason_does_not_say_no_data(db):
    """"Не показывается" = adj=0 с честной причиной, а не "нет данных" —
    ближайшая школа реально найдена и посчитана, просто далеко. Ключ
    должен считаться computed для confidence (см. compute_complex_
    location_score() — сравнение "нет данных" in reason)."""
    from bot.core.location_score import _schools_factor
    sid = await _insert_school("__test_school_far2__", 2000, "общеобразовательная")
    try:
        r = await _schools_factor(REF_LAT, REF_LON)
        assert r["adj"] == 0
        assert "нет данных" not in r["reason"]
    finally:
        await _cleanup_schools(sid)


@pytest.mark.asyncio
async def test_schools_no_row_returns_no_data_reason(db, monkeypatch):
    import bot.db.pg as pg_module

    async def _fake_fetchrow(*a, **kw):
        return None

    monkeypatch.setattr(pg_module, "fetchrow", _fake_fetchrow)
    from bot.core.location_score import _schools_factor
    r = await _schools_factor(REF_LAT, REF_LON)
    assert r["adj"] == 0
    assert "нет данных" in r["reason"]


# ── _kindergartens_factor ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_kindergartens_close(db):
    from bot.core.location_score import _kindergartens_factor
    kid = await _insert_kindergarten("__test_kg_close__", 250)
    try:
        r = await _kindergartens_factor(REF_LAT, REF_LON)
        assert r["adj"] == 2
    finally:
        await _cleanup_kindergartens(kid)


@pytest.mark.asyncio
async def test_kindergartens_mid(db):
    from bot.core.location_score import _kindergartens_factor
    kid = await _insert_kindergarten("__test_kg_mid__", 450)
    try:
        r = await _kindergartens_factor(REF_LAT, REF_LON)
        assert r["adj"] == 1
    finally:
        await _cleanup_kindergartens(kid)


@pytest.mark.asyncio
async def test_kindergartens_far_no_type_bonus_possible(db):
    """type в astana_kindergartens на 100% пустая — бонуса за тип для
    садиков нет структурно (не только по правилам, но и по данным)."""
    from bot.core.location_score import _kindergartens_factor
    kid = await _insert_kindergarten("__test_kg_far__", 900)
    try:
        r = await _kindergartens_factor(REF_LAT, REF_LON)
        assert r["adj"] == 0
        assert "нет данных" not in r["reason"]
    finally:
        await _cleanup_kindergartens(kid)


@pytest.mark.asyncio
async def test_kindergartens_no_row_returns_no_data_reason(db, monkeypatch):
    import bot.db.pg as pg_module

    async def _fake_fetchrow(*a, **kw):
        return None

    monkeypatch.setattr(pg_module, "fetchrow", _fake_fetchrow)
    from bot.core.location_score import _kindergartens_factor
    r = await _kindergartens_factor(REF_LAT, REF_LON)
    assert r["adj"] == 0
    assert "нет данных" in r["reason"]
