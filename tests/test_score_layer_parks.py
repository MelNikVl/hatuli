"""Регрессия для задачи 2026-08-15 ("детализация parks.py") —
bot/score_layers/parks.py::compute() теперь отдаёт не просто "парк
рядом", а расстояние до БЛИЖАЙШЕГО парка + его площадь в га (если
удалось её посчитать точечным Overpass-запросом геометрии по OSM id).
adj/пороги (400/700м) не менялись — регрессия и на них тоже.

fetch_poi/overpass_cached замоканы через monkeypatch (тот же приём, что
tests/test_location_score_no_double_school_count.py) — тест не бьёт в
реальный Overpass."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from bot.score_layers.parks import _polygon_area_m2


# ── _polygon_area_m2: чистая функция, без сети/БД ────────────────────────

def _rect_nodes(lat0=51.10, lon0=71.40, width_m=200.0, height_m=100.0):
    """Прямоугольник width_m x height_m (замкнутый way, как отдаёт Overpass
    `out geom` — первый узел повторён последним)."""
    lat_step = height_m / 111_000.0
    lon_step = width_m / (111_000.0 * 0.63)
    return [
        {"lat": lat0, "lon": lon0},
        {"lat": lat0, "lon": lon0 + lon_step},
        {"lat": lat0 + lat_step, "lon": lon0 + lon_step},
        {"lat": lat0 + lat_step, "lon": lon0},
        {"lat": lat0, "lon": lon0},
    ]


def test_polygon_area_m2_known_rectangle():
    area = _polygon_area_m2(_rect_nodes(width_m=200.0, height_m=100.0))
    assert area == pytest.approx(20_000.0, rel=1e-6)  # 200м x 100м = 2 га


def test_polygon_area_m2_too_few_points_is_none():
    assert _polygon_area_m2([{"lat": 1, "lon": 1}, {"lat": 2, "lon": 2}]) is None
    assert _polygon_area_m2([]) is None


def test_polygon_area_m2_skips_null_nodes():
    """Overpass иногда отдаёт null в geometry-списке (неразрешённый узел
    way) — не должно валить расчёт, просто не считается."""
    nodes = _rect_nodes()
    nodes.insert(2, None)
    area = _polygon_area_m2(nodes)
    assert area == pytest.approx(20_000.0, rel=1e-6)


# ── compute(): monkeypatch fetch_poi + overpass_cached ───────────────────

REF_LAT, REF_LON = 51.10, 71.40


def _park(dist_m, park_id=1, kind_type="way", lat=51.101, lon=71.402):
    return {"kind": "park", "dist_m": dist_m, "lat": lat, "lon": lon,
            "id": park_id, "type": kind_type}


@pytest.mark.asyncio
async def test_no_park_nearby(monkeypatch):
    import bot.score_layers.parks as parks_module

    async def _fake_fetch_poi(lat, lon):
        return []

    monkeypatch.setattr(parks_module, "fetch_poi", _fake_fetch_poi)
    adj, reason = await parks_module.compute({"lat": REF_LAT, "lon": REF_LON})
    assert adj == 0
    assert "нет" in reason


@pytest.mark.asyncio
async def test_osm_unavailable(monkeypatch):
    import bot.score_layers.parks as parks_module

    async def _fake_fetch_poi(lat, lon):
        return None

    monkeypatch.setattr(parks_module, "fetch_poi", _fake_fetch_poi)
    adj, reason = await parks_module.compute({"lat": REF_LAT, "lon": REF_LON})
    assert adj == 0
    assert reason == "OSM недоступен"


@pytest.mark.asyncio
async def test_no_coords():
    import bot.score_layers.parks as parks_module
    adj, reason = await parks_module.compute({})
    assert adj == 0
    assert reason == "нет координат"


@pytest.mark.asyncio
async def test_park_area_included_when_geometry_available(monkeypatch):
    import bot.score_layers.parks as parks_module
    import bot.score_layers.osm as osm_module

    async def _fake_fetch_poi(lat, lon):
        return [_park(350, park_id=42)]

    async def _fake_overpass_cached(lat, lon, kind, query):
        assert kind == "park_geom"
        assert "42" in query
        return {"elements": [{"type": "way", "id": 42, "geometry": _rect_nodes(width_m=300, height_m=200)}]}

    monkeypatch.setattr(parks_module, "fetch_poi", _fake_fetch_poi)
    monkeypatch.setattr(osm_module, "overpass_cached", _fake_overpass_cached)

    adj, reason = await parks_module.compute({"lat": REF_LAT, "lon": REF_LON})
    assert adj == 2  # 350м <= 400м
    assert "350м" in reason
    assert "6.0 га" in reason  # 300м x 200м = 6 га


@pytest.mark.asyncio
async def test_area_omitted_when_nearest_park_is_a_point_node(monkeypatch):
    """Ближайший парк размечен точкой (node), не полигоном — площади у
    точки нет в принципе, reason честно остаётся без неё (Unknown ≠
    average), геометрию даже не запрашиваем."""
    import bot.score_layers.parks as parks_module
    import bot.score_layers.osm as osm_module

    async def _fake_fetch_poi(lat, lon):
        return [_park(200, park_id=7, kind_type="node")]

    called = {"n": 0}

    async def _fake_overpass_cached(lat, lon, kind, query):
        called["n"] += 1
        return None

    monkeypatch.setattr(parks_module, "fetch_poi", _fake_fetch_poi)
    monkeypatch.setattr(osm_module, "overpass_cached", _fake_overpass_cached)

    adj, reason = await parks_module.compute({"lat": REF_LAT, "lon": REF_LON})
    assert adj == 2
    assert reason == "парк в 200м"
    assert "га" not in reason
    assert called["n"] == 0  # точечный запрос геометрии не тратится впустую


@pytest.mark.asyncio
async def test_area_omitted_when_geometry_fetch_fails(monkeypatch):
    """way есть, но дозапрос геометрии не удался (Overpass лёг/таймаут) —
    falls back на расстояние-без-площади, не роняет весь фактор."""
    import bot.score_layers.parks as parks_module
    import bot.score_layers.osm as osm_module

    async def _fake_fetch_poi(lat, lon):
        return [_park(500, park_id=99)]

    async def _fake_overpass_cached(lat, lon, kind, query):
        return None

    monkeypatch.setattr(parks_module, "fetch_poi", _fake_fetch_poi)
    monkeypatch.setattr(osm_module, "overpass_cached", _fake_overpass_cached)

    adj, reason = await parks_module.compute({"lat": REF_LAT, "lon": REF_LON})
    assert adj == 1  # 500м > 400м
    assert reason == "парк в 500м"


@pytest.mark.asyncio
async def test_picks_nearest_of_several_parks_for_area_query(monkeypatch):
    import bot.score_layers.parks as parks_module
    import bot.score_layers.osm as osm_module

    async def _fake_fetch_poi(lat, lon):
        return [_park(600, park_id=1), _park(150, park_id=2), _park(390, park_id=3)]

    queried_ids = []

    async def _fake_overpass_cached(lat, lon, kind, query):
        queried_ids.append(query)
        return {"elements": [{"type": "way", "id": 2, "geometry": _rect_nodes()}]}

    monkeypatch.setattr(parks_module, "fetch_poi", _fake_fetch_poi)
    monkeypatch.setattr(osm_module, "overpass_cached", _fake_overpass_cached)

    adj, reason = await parks_module.compute({"lat": REF_LAT, "lon": REF_LON})
    assert "150м" in reason
    assert len(queried_ids) == 1
    assert "way(2)" in queried_ids[0]  # именно ближайший (id=2), не все три


@pytest.mark.asyncio
async def test_adj_thresholds_unchanged(monkeypatch):
    import bot.score_layers.parks as parks_module
    import bot.score_layers.osm as osm_module

    async def _fake_overpass_cached(lat, lon, kind, query):
        return None

    monkeypatch.setattr(osm_module, "overpass_cached", _fake_overpass_cached)

    async def _near(lat, lon):
        return [_park(400)]

    async def _far(lat, lon):
        return [_park(699)]

    monkeypatch.setattr(parks_module, "fetch_poi", _near)
    adj, _ = await parks_module.compute({"lat": REF_LAT, "lon": REF_LON})
    assert adj == 2

    monkeypatch.setattr(parks_module, "fetch_poi", _far)
    adj, _ = await parks_module.compute({"lat": REF_LAT, "lon": REF_LON})
    assert adj == 1
