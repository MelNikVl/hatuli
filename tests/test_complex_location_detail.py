"""Регрессия для Фазы L2 продуктового трека «Локация» (docs/location_
product_design.md, план L2, задача 2026-08-14), коммит 1 — bot/core/
complex_location_detail.py::build_complex_location_detail() и роут
/admin/api/complex/{id}/location-detail.

Дополняет (не меняет) живой /admin/api/complex/{id}/location-score —
регрессия того эндпойнта не в этом файле, см. tests/test_house_
resolution_geo.py (по-прежнему зелёный без изменений).

Координаты тестовых ЖК — заведомо далеко от Астаны (тот же приём, что
tests/test_hex_market_stats_snapshot.py и tests/test_complex_location_
score_snapshot.py) — не пересекаются ни с реальными hex_market_stats/
demolition_houses, ни с предыдущими тестовыми районами этой сессии.
fetch_poi()/fetch_schools_poi() подменяются фейком — тест не бьёт в
реальный Overpass."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio
from datetime import date, timedelta
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

# Далеко от Астаны и от других тестовых зон этой сессии (1.x/2.x/5.x уже
# использованы в test_hex_market_stats_snapshot.py/test_complex_
# location_score_snapshot.py) — своя ничейная область.
_LAT, _LON = 10.1500, 10.4500


@pytest_asyncio.fixture
async def db():
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    yield
    await close_pool()


@pytest.fixture(autouse=True)
def _fixed_hex_edge_m():
    """Тот же живой урок, что уже пойман в test_hex_market_stats_
    snapshot.py — module-level _cache в bot/db/settings персистентен
    между тестами одного pytest-процесса, HEX_EDGE_M на проде реально
    100, не дефолт 50."""
    from bot.db import settings as app_settings
    had_key = "HEX_EDGE_M" in app_settings._cache
    prev = app_settings._cache.get("HEX_EDGE_M")
    app_settings._cache["HEX_EDGE_M"] = "50"
    yield
    if had_key:
        app_settings._cache["HEX_EDGE_M"] = prev
    else:
        app_settings._cache.pop("HEX_EDGE_M", None)


async def _insert_complex_with_listing(name, lat=_LAT, lon=_LON):
    from bot.db.pg import fetchval, execute
    cid = await fetchval("INSERT INTO complexes (name) VALUES ($1) RETURNING id", name)
    lid = f"__test_cld_listing_{cid}__"
    await execute(
        "INSERT INTO apartment_listings (id, complex_name, lat, lon, price, area, rooms, is_active) "
        "VALUES ($1, $2, $3, $4, 30000000, 60.0, 2, TRUE)",
        lid, name, lat, lon)
    return cid, lid


async def _cleanup_complex(cid, lid):
    from bot.db.pg import execute
    await execute("DELETE FROM complex_location_scores WHERE complex_id=$1", cid)
    await execute("DELETE FROM complex_stats_history WHERE complex_id=$1", cid)
    await execute("DELETE FROM apartment_listings WHERE id=$1", lid)
    await execute("DELETE FROM complexes WHERE id=$1", cid)


def _fake_poi(monkeypatch, poi_points=None, school_points=None):
    import bot.core.complex_location_detail as mod

    async def _fake_fetch_poi(lat, lon):
        return poi_points if poi_points is not None else []

    async def _fake_fetch_schools_poi(lat, lon):
        return school_points if school_points is not None else []

    import bot.score_layers.poi as poi_module
    import bot.score_layers.schools as schools_module
    monkeypatch.setattr(poi_module, "fetch_poi", _fake_fetch_poi)
    monkeypatch.setattr(schools_module, "fetch_schools_poi", _fake_fetch_schools_poi)


@pytest.mark.asyncio
async def test_complex_not_found_raises(db):
    from bot.core.complex_location_detail import build_complex_location_detail, ComplexNotFound
    with pytest.raises(ComplexNotFound):
        await build_complex_location_detail(999_999_999)


@pytest.mark.asyncio
async def test_no_coords_returns_honest_empty_shape(db, monkeypatch):
    from bot.db.pg import fetchval, execute
    from bot.core.complex_location_detail import build_complex_location_detail

    _fake_poi(monkeypatch)
    cid = await fetchval("INSERT INTO complexes (name) VALUES ('__test_cld_nocoords__') RETURNING id")
    try:
        result = await build_complex_location_detail(cid)
        assert result["has_coords"] is False
        assert result["has_score"] is False
        assert result["score"] is None
        assert result["density"] == []
        assert result["demolition"] == []
        assert result["price_drop_trend"] == []
    finally:
        await execute("DELETE FROM complexes WHERE id=$1", cid)


@pytest.mark.asyncio
async def test_has_score_false_when_no_row_unknown_not_average(db, monkeypatch):
    """Unknown != average — нет строки в complex_location_scores (backfill
    не дошёл) -> has_score=False, НЕ фейковый score=0/50."""
    from bot.core.complex_location_detail import build_complex_location_detail

    _fake_poi(monkeypatch)
    cid, lid = await _insert_complex_with_listing("__test_cld_no_score_row__")
    try:
        result = await build_complex_location_detail(cid)
        assert result["has_coords"] is True
        assert result["has_score"] is False
        assert result["score"] is None
    finally:
        await _cleanup_complex(cid, lid)


@pytest.mark.asyncio
async def test_has_score_true_parses_breakdown(db, monkeypatch):
    from bot.db.pg import execute
    from bot.core.complex_location_detail import build_complex_location_detail
    import json

    _fake_poi(monkeypatch)
    cid, lid = await _insert_complex_with_listing("__test_cld_with_score__")
    try:
        breakdown = {"transport": {"lrt_access": {"adj": 4}}, "informational": {"bank": {"adj": 0}}}
        await execute("""
            INSERT INTO complex_location_scores
                (complex_id, score, confidence, transport_score, infra_score,
                 noise_score, green_score, risk_score, breakdown, score_version)
            VALUES ($1, 70, 85, 4, 3, -1, 2, 0, $2::jsonb, 'loc_v1')
        """, cid, json.dumps(breakdown, ensure_ascii=False))

        result = await build_complex_location_detail(cid)
        assert result["has_score"] is True
        score = result["score"]
        assert score["score"] == 70
        assert score["confidence"] == 85
        assert score["transport_score"] == 4
        assert score["breakdown"]["transport"]["lrt_access"]["adj"] == 4
        assert score["computed_at"] is not None
    finally:
        await _cleanup_complex(cid, lid)


@pytest.mark.asyncio
async def test_density_includes_center_hex_and_neighbors(db, monkeypatch):
    from bot.db.pg import execute
    from bot.core.complex_location_detail import build_complex_location_detail
    from bot.core.hexgrid import hex_id as compute_hex_id, neighbors as hex_neighbors

    _fake_poi(monkeypatch)
    cid, lid = await _insert_complex_with_listing("__test_cld_density__")
    center = compute_hex_id(_LAT, _LON, 50.0)
    a_neighbor = hex_neighbors(center)[0]
    try:
        await execute(
            "INSERT INTO hex_market_stats (hex_id, date, edge_m, listings_count, avg_price_m2) "
            "VALUES ($1, CURRENT_DATE, 50, 12, 500000)", center)
        await execute(
            "INSERT INTO hex_market_stats (hex_id, date, edge_m, listings_count, avg_price_m2) "
            "VALUES ($1, CURRENT_DATE, 50, 3, 480000)", a_neighbor)

        result = await build_complex_location_detail(cid)
        by_hex = {d["hex_id"]: d for d in result["density"]}
        assert len(result["density"]) == 7  # центр + 6 соседей (кольцо)
        assert by_hex[center]["is_center"] is True
        assert by_hex[center]["listings_count"] == 12
        assert by_hex[a_neighbor]["listings_count"] == 3
        assert by_hex[a_neighbor]["is_center"] is False
        assert len(by_hex[center]["corners"]) == 6
        # Гексагон без снимка -> честно None, не 0
        other_neighbors = [hid for hid in by_hex if hid not in (center, a_neighbor)]
        assert by_hex[other_neighbors[0]]["listings_count"] is None
    finally:
        await execute("DELETE FROM hex_market_stats WHERE hex_id IN ($1, $2)", center, a_neighbor)
        await _cleanup_complex(cid, lid)


@pytest.mark.asyncio
async def test_demolition_only_within_radius_sorted_by_distance(db, monkeypatch):
    from bot.db.pg import execute
    from bot.core.complex_location_detail import build_complex_location_detail

    _fake_poi(monkeypatch)
    cid, lid = await _insert_complex_with_listing("__test_cld_demolition__")
    near_addr, far_addr = "__test дом рядом__", "__test дом далеко__"
    try:
        # ~300м к северу — в радиусе 1км
        await execute(
            "INSERT INTO demolition_houses (address, demolish_year, lat, lon) VALUES ($1, 2028, $2, $3)",
            near_addr, _LAT + 0.0027, _LON)
        # ~50км — вне радиуса
        await execute(
            "INSERT INTO demolition_houses (address, demolish_year, lat, lon) VALUES ($1, 2029, $2, $3)",
            far_addr, _LAT + 0.45, _LON)

        result = await build_complex_location_detail(cid)
        addrs = [d["address"] for d in result["demolition"]]
        assert near_addr in addrs
        assert far_addr not in addrs
        assert result["demolition"][0]["address"] == near_addr
        assert result["demolition"][0]["dist_m"] < 1000
    finally:
        await execute("DELETE FROM demolition_houses WHERE address IN ($1, $2)", near_addr, far_addr)
        await _cleanup_complex(cid, lid)


@pytest.mark.asyncio
async def test_poi_grouped_by_kind_from_cache_aware_fetch(db, monkeypatch):
    from bot.core.complex_location_detail import build_complex_location_detail

    _fake_poi(
        monkeypatch,
        poi_points=[{"kind": "bus_stop", "dist_m": 100, "lat": _LAT, "lon": _LON},
                    {"kind": "park", "dist_m": 200, "lat": _LAT + 0.001, "lon": _LON}],
        school_points=[{"kind": "school", "lat": _LAT + 0.002, "lon": _LON}],
    )
    cid, lid = await _insert_complex_with_listing("__test_cld_poi__")
    try:
        result = await build_complex_location_detail(cid)
        assert len(result["poi"]["bus_stop"]) == 1
        assert len(result["poi"]["park"]) == 1
        assert len(result["poi"]["school"]) == 1
        assert result["poi"]["shop"] == []  # ключ есть, пустой список — не отсутствует
    finally:
        await _cleanup_complex(cid, lid)


@pytest.mark.asyncio
async def test_price_drop_trend_ordered_by_date(db, monkeypatch):
    from bot.db.pg import execute
    from bot.core.complex_location_detail import build_complex_location_detail

    _fake_poi(monkeypatch)
    cid, lid = await _insert_complex_with_listing("__test_cld_trend__")
    d0, d1 = date.today() - timedelta(days=2), date.today() - timedelta(days=1)
    try:
        await execute(
            "INSERT INTO complex_stats_history (complex_id, date, price_drop_share_30d) VALUES ($1, $2, 0.4)",
            cid, d0)
        await execute(
            "INSERT INTO complex_stats_history (complex_id, date, price_drop_share_30d) VALUES ($1, $2, 0.1)",
            cid, d1)

        result = await build_complex_location_detail(cid)
        trend = result["price_drop_trend"]
        assert len(trend) == 2
        assert trend[0]["share"] == pytest.approx(0.4)
        assert trend[1]["share"] == pytest.approx(0.1)
        assert trend[0]["date"] < trend[1]["date"]
    finally:
        await _cleanup_complex(cid, lid)


@pytest.mark.asyncio
async def test_route_returns_404_for_missing_complex(db):
    import httpx
    from bot.db.compat import BotDB
    from bot.admin_web import create_admin_app

    admin_password = os.getenv("ADMIN_PASSWORD", "admin123")
    db_path = os.getenv("DB_PATH", "bot.db")
    bdb = BotDB(db_path)
    await bdb.init()
    app = create_admin_app(bdb, admin_password, "test", db_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/admin/api/complex/999999999/location-detail")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_route_returns_full_shape_for_existing_complex(db, monkeypatch):
    import httpx
    from bot.db.compat import BotDB
    from bot.admin_web import create_admin_app

    _fake_poi(monkeypatch)
    cid, lid = await _insert_complex_with_listing("__test_cld_route__")
    try:
        admin_password = os.getenv("ADMIN_PASSWORD", "admin123")
        db_path = os.getenv("DB_PATH", "bot.db")
        bdb = BotDB(db_path)
        await bdb.init()
        app = create_admin_app(bdb, admin_password, "test", db_path)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get(f"/admin/api/complex/{cid}/location-detail")
        assert r.status_code == 200
        body = r.json()
        assert body["has_coords"] is True
        assert body["has_score"] is False
        assert "density" in body and "demolition" in body and "poi" in body
    finally:
        await _cleanup_complex(cid, lid)
