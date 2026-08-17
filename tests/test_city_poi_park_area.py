"""Тесты для задачи 2026-08-17 ("Parks — исправить потерю площади после
перехода на локальные точки"): scripts/sync_city_poi.py считает площадь
парков-way ОДНИМ batch-запросом при синхронизации и кладёт в
city_poi.extra (JSONB, миграция не нужна — колонка уже была); bot/
score_layers/poi.py::fetch_poi читает её оттуда; bot/score_layers/
parks.py::_nearest_park_area_ha использует area_ha напрямую БЕЗ
Overpass, если он уже есть."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import pytest
import pytest_asyncio
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

SYN_LAT = 20.0000
SYN_LON = 20.0000


@pytest_asyncio.fixture
async def db():
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    yield
    await close_pool()


def _way_geom_rect(way_id, width_m=200.0, height_m=100.0, lat0=1.0, lon0=1.0):
    lat_step = height_m / 111_000.0
    lon_step = width_m / (111_000.0 * 0.63)
    return {"type": "way", "id": way_id, "geometry": [
        {"lat": lat0, "lon": lon0},
        {"lat": lat0, "lon": lon0 + lon_step},
        {"lat": lat0 + lat_step, "lon": lon0 + lon_step},
        {"lat": lat0 + lat_step, "lon": lon0},
        {"lat": lat0, "lon": lon0},
    ]}


# ── sync_city_poi.py: pure logic (без сети/БД) ───────────────────────────

def test_extract_points_with_osm_ref_captures_type_and_id():
    from sync_city_poi import _extract_points
    data = {"elements": [{"type": "way", "id": 555, "lat": 1.0, "lon": 2.0, "tags": {}}]}
    points = _extract_points("park", data, with_osm_ref=True)
    assert points[0]["osm_type"] == "way"
    assert points[0]["osm_id"] == 555


def test_extract_points_without_osm_ref_omits_keys():
    """Дефолт (with_osm_ref=False, все kind кроме park) — поведение НЕ
    меняется, osm_type/osm_id не появляются в точке вообще."""
    from sync_city_poi import _extract_points
    data = {"elements": [{"type": "node", "id": 1, "lat": 1.0, "lon": 2.0, "tags": {}}]}
    points = _extract_points("shop", data)
    assert "osm_type" not in points[0]
    assert "osm_id" not in points[0]


def test_compute_park_areas_ha_known_rectangle():
    from sync_city_poi import _compute_park_areas_ha
    geom_data = {"elements": [_way_geom_rect(42, width_m=300.0, height_m=200.0)]}
    areas = _compute_park_areas_ha(geom_data)
    assert areas[42] == pytest.approx(6.0, rel=1e-3)  # 300x200м = 6 га


def test_compute_park_areas_ha_skips_nodes_and_degenerate():
    from sync_city_poi import _compute_park_areas_ha
    geom_data = {"elements": [
        {"type": "node", "id": 1, "lat": 1.0, "lon": 1.0},           # не way
        {"type": "way", "id": 2, "geometry": [{"lat": 1.0, "lon": 1.0}]},  # < 3 точек
        _way_geom_rect(3),
    ]}
    areas = _compute_park_areas_ha(geom_data)
    assert 1 not in areas
    assert 2 not in areas
    assert 3 in areas


def test_extra_json_none_when_no_optional_keys():
    """Не-park точки (без osm_type/osm_id/area_ha) -> extra=NULL, как
    было ДО этой задачи — поведение остальных 10+ kind не меняется."""
    from sync_city_poi import _extra_json
    assert _extra_json({"kind": "shop", "lat": 1, "lon": 2, "name": None, "address": None}) is None


def test_extra_json_includes_only_present_optional_keys():
    from sync_city_poi import _extra_json
    import json
    p = {"kind": "park", "lat": 1, "lon": 2, "osm_type": "way", "osm_id": 7, "area_ha": 3.5}
    extra = json.loads(_extra_json(p))
    assert extra == {"osm_type": "way", "osm_id": 7, "area_ha": 3.5}


def test_extra_json_omits_none_area_ha():
    """Точечный (node) парк — osm_type/osm_id есть, area_ha нет (точка,
    площади не бывает) -> extra содержит только то, что реально есть."""
    from sync_city_poi import _extra_json
    import json
    p = {"kind": "park", "lat": 1, "lon": 2, "osm_type": "node", "osm_id": 9, "area_ha": None}
    extra = json.loads(_extra_json(p))
    assert extra == {"osm_type": "node", "osm_id": 9}


# ── fetch_kind("park") merge: точки + площадь одним доп. запросом ────────

@pytest.mark.asyncio
async def test_fetch_park_points_with_area_merges_by_id(monkeypatch):
    import sync_city_poi as sync_module

    point_data = {"elements": [
        {"type": "way", "id": 42, "lat": 1.0005, "lon": 1.0005, "tags": {"name": "Парк А"}},
        {"type": "node", "id": 99, "lat": 2.0, "lon": 2.0, "tags": {"name": "Сквер Б"}},
    ]}
    geom_data = {"elements": [_way_geom_rect(42, width_m=300.0, height_m=200.0)]}

    calls = []

    async def _fake_overpass_request(query):
        calls.append(query)
        return geom_data

    monkeypatch.setattr(sync_module, "_overpass_request", _fake_overpass_request)
    points = await sync_module._fetch_park_points_with_area(point_data)

    by_id = {p.get("osm_id"): p for p in points}
    assert by_id[42]["area_ha"] == pytest.approx(6.0, rel=1e-3)
    assert "area_ha" not in by_id[99] or by_id[99].get("area_ha") is None  # node — без площади
    assert len(calls) == 1  # ОДИН batch-запрос геометрии, не по одному на парк


@pytest.mark.asyncio
async def test_fetch_park_points_with_area_geometry_failure_keeps_points(monkeypatch):
    """Гео-запрос не удался (Overpass лёг) -> точки ВСЁ РАВНО возвращаются
    (без area_ha) — позиция важнее площади, не роняем всю категорию."""
    import sync_city_poi as sync_module

    point_data = {"elements": [
        {"type": "way", "id": 42, "lat": 1.0, "lon": 1.0, "tags": {"name": "Парк А"}},
    ]}

    async def _fake_overpass_request(query):
        return None

    monkeypatch.setattr(sync_module, "_overpass_request", _fake_overpass_request)
    points = await sync_module._fetch_park_points_with_area(point_data)
    assert len(points) == 1
    assert points[0].get("area_ha") is None


# ── local_poi_near / fetch_poi / _nearest_park_area_ha — сквозной путь ──

@pytest_asyncio.fixture
async def synced_park_with_area(db):
    """Один way-парк в city_poi с area_ha в extra — как после реального
    sync_city_poi.py --category parks.

    kind РЕАЛЬНЫЙ ('park', не '__test_syn_park__') — намеренно: fetch_poi()
    жёстко читает _LOCAL_KIND_MAP["park"]="park", подменить это без
    monkeypatch внутренностей poi.py нельзя, а сквозной тест (test_fetch_
    poi_local_park_carries_area_ha) должен проверять РЕАЛЬНЫЙ путь.
    Поэтому cleanup ОБЯЗАН быть по координатам (SYN_LAT/SYN_LON — заведомо
    вне Астаны), НЕ по kind — DELETE ... WHERE kind='park' без условия на
    координаты снёс бы ВСЕ реальные припаркованные (реальные, не тестовые)
    строки park (см. инцидент 2026-08-17: ровно эта ошибка в первой
    версии этого файла стёрла 173 живых записи park, восстановлены из
    backups/city_poi_backup_20260817_pre_roads.sql — HE повторять)."""
    from bot.db.pg import execute
    await execute(
        """INSERT INTO city_poi (kind, name, lat, lon, extra, updated_at)
           VALUES ('park', 'Тестовый парк', $1, $2, $3::jsonb, now())""",
        SYN_LAT, SYN_LON, '{"osm_type": "way", "osm_id": 777, "area_ha": 4.2}',
    )
    yield
    await execute("DELETE FROM city_poi WHERE kind = 'park' AND lat = $1 AND lon = $2",
                  SYN_LAT, SYN_LON)


@pytest.mark.asyncio
async def test_local_poi_near_returns_extra(synced_park_with_area):
    from bot.score_layers.osm import local_poi_near
    found = await local_poi_near(SYN_LAT, SYN_LON, ["park"], 700)
    assert found is not None and len(found) == 1
    assert found[0]["extra"] == {"osm_type": "way", "osm_id": 777, "area_ha": 4.2}


@pytest.mark.asyncio
async def test_fetch_poi_local_park_carries_area_ha(synced_park_with_area):
    from bot.score_layers.poi import fetch_poi
    result = await fetch_poi(SYN_LAT, SYN_LON)
    assert result is not None
    park = next(p for p in result if p["kind"] == "park")
    assert park["area_ha"] == pytest.approx(4.2)
    assert park["id"] == 777
    assert park["type"] == "way"


@pytest.mark.asyncio
async def test_nearest_park_area_ha_uses_local_value_without_overpass(monkeypatch):
    """area_ha уже в park (из local-источника) -> _nearest_park_area_ha
    возвращает его НАПРЯМУЮ, overpass_cached НЕ вызывается вообще —
    ключевая регрессия задачи ("не возвращать live Overpass в
    критический путь")."""
    import bot.score_layers.parks as parks_module
    import bot.score_layers.osm as osm_module

    called = {"n": 0}

    async def _fake_overpass_cached(*a, **kw):
        called["n"] += 1
        return None

    monkeypatch.setattr(osm_module, "overpass_cached", _fake_overpass_cached)
    area = await parks_module._nearest_park_area_ha(
        {"type": "way", "id": 777, "lat": SYN_LAT, "lon": SYN_LON, "area_ha": 4.2})
    assert area == pytest.approx(4.2)
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_nearest_park_area_ha_falls_back_to_live_when_area_ha_absent(monkeypatch):
    """area_ha отсутствует (park пришёл с редкого live-Overpass фолбэка
    fetch_poi, не из city_poi) -> старый live-путь по id ещё работает,
    не удалён, просто больше не единственный/не основной."""
    import bot.score_layers.parks as parks_module
    import bot.score_layers.osm as osm_module
    from tests.test_score_layer_parks import _rect_nodes

    async def _fake_overpass_cached(lat, lon, kind, query):
        assert kind == "park_geom"
        return {"elements": [{"type": "way", "id": 42, "geometry": _rect_nodes(width_m=300, height_m=200)}]}

    monkeypatch.setattr(osm_module, "overpass_cached", _fake_overpass_cached)
    area = await parks_module._nearest_park_area_ha(
        {"type": "way", "id": 42, "lat": 1.0, "lon": 1.0})
    assert area == pytest.approx(6.0, rel=1e-3)
