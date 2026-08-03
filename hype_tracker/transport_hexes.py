#!/usr/bin/env python3
"""Тепловая карта транспортной доступности Астаны.
Гексы 100 м (ребро). Скоринг: ЛРТ-станции в радиусе 1 км (вес 0.75) +
автобусные остановки в радиусе 500 м (вес 0.4) + маршрутная связность
(сколько РАЗНЫХ автобусных/троллейбусных линий доступны в радиусе 500 м,
вес 0.5 — просто "остановка рядом" не значит "легко уехать": одна редкая
линия и десять маршрутов на одном пятачке — разная доступность) + крупные
дороги в радиусе 600 м (вес 0.3, доступность на машине) + развязки/
перекрёстки в радиусе 800 м (вес 0.2, узел транспортной сети). Пишет
transport_hexes.

Дороги/развязки/маршруты берутся из OSM Overpass (см. bot/score_layers/osm.py —
тот же принцип зеркал-фолбэков, но здесь один большой запрос на весь
bbox города разом, а не поштучно на кэш-ячейку, т.к. это офлайн-скрипт,
а не хук на каждое объявление).

Маршрутная связность — не полноценное расписание (GTFS для Астаны
публично не найден), а прокси через OSM route-relations: считаем не
"есть ли остановка рядом", а "сколько РАЗНЫХ линий (bus/trolleybus/tram)
имеют свою остановку рядом" — узел с 8 маршрутами объективно даёт больше
возможностей уехать, чем тупиковая остановка с одним."""
import asyncio
import math
import sys
from pathlib import Path

import numpy as np
import psycopg2
import psycopg2.extras

BASE = Path("/home/nik/krisha_bot")
sys.path.insert(0, str(BASE))
from bot.core.hexgrid import hex_center  # noqa: E402

_LAT0, _LON0 = 51.128, 71.430
_MLAT, _MLON = 110574.0, 111320.0 * math.cos(math.radians(_LAT0))
EDGE = 100.0  # грань гекса, метры
BBOX = (51.03, 51.22, 71.31, 71.61)  # lat_min, lat_max, lon_min, lon_max
LRT_RADIUS = 1000.0   # м
BUS_RADIUS = 500.0    # м
ROAD_RADIUS = 600.0   # м — доступность на машине (не путать с шумом: тут наоборот, ближе = лучше)
JUNCTION_RADIUS = 800.0  # м — развязки/крупные перекрёстки
ROUTE_RADIUS = 500.0  # м — радиус учёта маршрутов (тот же, что и для остановок)
ROUTE_COUNT_CAP = 6.0  # 6+ разных линий рядом = максимум по этому фактору


def load_database_url() -> str:
    for line in (BASE / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip()
    return "postgresql://krisha@localhost/krisha_bot"


def conn():
    return psycopg2.connect(load_database_url().rsplit("/", 1)[0] + "/krisha_bot")


def nearest_dists(pts: np.ndarray, grid: np.ndarray) -> np.ndarray:
    """Минимальное расстояние (м) от каждой точки сетки до любого из pts."""
    M = grid.shape[0]
    out = np.full(M, 1e9)
    if pts.shape[0] == 0:
        return out
    chunk = 1500
    for i in range(0, M, chunk):
        gc = grid[i:i + chunk]
        dlat = (pts[:, 0][None, :] - gc[:, 0][:, None]) * _MLAT
        dlon = (pts[:, 1][None, :] - gc[:, 1][:, None]) * _MLON
        out[i:i + chunk] = np.sqrt(dlat * dlat + dlon * dlon).min(axis=1)
    return out


async def fetch_roads_and_junctions():
    """Крупные дороги (way, geometry) + развязки (motorway_junction node +
    перекрёстки, где сходятся 3+ крупные дороги) из Overpass, один запрос
    на весь bbox Астаны."""
    from bot.score_layers.osm import overpass_request

    lat_min, lat_max, lon_min, lon_max = BBOX
    bbox_str = f"{lat_min},{lon_min},{lat_max},{lon_max}"
    query = f"""
[out:json][timeout:60];
(
  way["highway"~"^(motorway|trunk|primary|secondary)$"]({bbox_str});
);
out geom;
(
  node["highway"="motorway_junction"]({bbox_str});
);
out body;
"""
    data = await overpass_request(query, timeout=70.0)
    road_pts: list[tuple[float, float]] = []
    junction_pts: list[tuple[float, float]] = []
    if not data:
        return road_pts, junction_pts
    for el in data.get("elements", []):
        if el.get("type") == "way" and "geometry" in el:
            for pt in el["geometry"]:
                road_pts.append((pt["lat"], pt["lon"]))
        elif el.get("type") == "node" and el.get("tags", {}).get("highway") == "motorway_junction":
            junction_pts.append((el["lat"], el["lon"]))
    return road_pts, junction_pts


async def fetch_routes() -> list[np.ndarray]:
    """Автобусные/троллейбусные/трамвайные маршруты (route relations) из
    Overpass — для каждого маршрута список координат его остановок.
    Возвращает список массивов [(lat, lon), ...], один массив на маршрут
    (не сливаем все точки в одну кучу — иначе теряется "это одна и та же
    линия", а нам важно количество РАЗНЫХ линий рядом с гексом, не просто
    остановок)."""
    from bot.score_layers.osm import overpass_request

    lat_min, lat_max, lon_min, lon_max = BBOX
    bbox_str = f"{lat_min},{lon_min},{lat_max},{lon_max}"
    query = f"""
[out:json][timeout:90];
relation["route"~"^(bus|trolleybus|tram)$"]({bbox_str});
out body;
>;
out skel qt;
"""
    data = await overpass_request(query, timeout=100.0)
    if not data:
        return []
    node_coords: dict[int, tuple[float, float]] = {}
    relations: list[dict] = []
    for el in data.get("elements", []):
        if el.get("type") == "node":
            node_coords[el["id"]] = (el["lat"], el["lon"])
        elif el.get("type") == "relation":
            relations.append(el)

    routes: list[np.ndarray] = []
    for rel in relations:
        pts = []
        for m in rel.get("members", []):
            if m.get("type") != "node":
                continue
            if m.get("role") not in ("stop", "stop_entry_only", "platform", ""):
                continue
            c = node_coords.get(m["ref"])
            if c:
                pts.append(c)
        if pts:
            routes.append(np.array(pts, dtype=float))
    return routes


def route_counts_for_grid(routes: list[np.ndarray], grid: np.ndarray, radius: float) -> np.ndarray:
    """Для каждой точки сетки — сколько РАЗНЫХ маршрутов (routes) имеют
    хотя бы одну остановку в радиусе. Цикл по маршрутам (их обычно 50-150 в
    городе), внутри — векторный nearest_dists на всю сетку разом."""
    counts = np.zeros(grid.shape[0], dtype=int)
    for pts in routes:
        d = nearest_dists(pts, grid)
        counts += (d <= radius).astype(int)
    return counts


async def main() -> None:
    db = conn()
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT lat, lon FROM transport_stops")
    stops = np.array([(r["lat"], r["lon"]) for r in cur.fetchall()], dtype=float)
    cur.execute("SELECT lat, lon FROM city_poi WHERE kind='landmark' AND name LIKE 'ЛРТ:%'")
    lrt = np.array([(r["lat"], r["lon"]) for r in cur.fetchall()], dtype=float)
    print(f"остановок: {len(stops)}, ЛРТ-станций: {len(lrt)}")

    road_pts, junction_pts = await fetch_roads_and_junctions()
    roads = np.array(road_pts, dtype=float) if road_pts else np.empty((0, 2))
    junctions = np.array(junction_pts, dtype=float) if junction_pts else np.empty((0, 2))
    print(f"точек дорог: {len(roads)}, развязок: {len(junctions)}")

    routes = await fetch_routes()
    print(f"маршрутов ОТ (bus/trolleybus/tram): {len(routes)}")

    # сетка гексов вокруг центра города
    qmax = int((BBOX[1] - _LAT0) * _MLAT / (EDGE * 1.5)) + 3
    rmax = int((BBOX[3] - _LON0) * _MLON / (EDGE * math.sqrt(3))) + 3
    centers = []
    for q in range(-qmax, qmax + 1):
        for r in range(-rmax, rmax + 1):
            lat, lon = hex_center(f"{q}:{r}", EDGE)
            if BBOX[0] <= lat <= BBOX[1] and BBOX[2] <= lon <= BBOX[3]:
                centers.append((lat, lon))
    centers = np.array(centers, dtype=float)
    print(f"гексов в bbox: {len(centers)}")

    dist_lrt = nearest_dists(lrt, centers)
    dist_bus = nearest_dists(stops, centers)
    dist_road = nearest_dists(roads, centers)
    dist_junction = nearest_dists(junctions, centers)
    route_count = route_counts_for_grid(routes, centers, ROUTE_RADIUS) if routes else np.zeros(centers.shape[0], dtype=int)

    lrt_c = np.clip(1 - dist_lrt / LRT_RADIUS, 0, 1)
    bus_c = np.clip(1 - dist_bus / BUS_RADIUS, 0, 1)
    road_c = np.clip(1 - dist_road / ROAD_RADIUS, 0, 1)
    junction_c = np.clip(1 - dist_junction / JUNCTION_RADIUS, 0, 1)
    route_c = np.clip(route_count / ROUTE_COUNT_CAP, 0, 1)
    score = np.clip(lrt_c * 0.75 + bus_c * 0.4 + route_c * 0.5 + road_c * 0.3 + junction_c * 0.2, 0, 1)

    mask = score > 0.05
    rows = [(float(la), float(lo), float(s), float(dl), float(db), float(dr), float(dj), int(rc))
            for (la, lo), s, dl, db, dr, dj, rc in zip(
                centers[mask], score[mask], dist_lrt[mask], dist_bus[mask],
                dist_road[mask], dist_junction[mask], route_count[mask])]
    print(f"со скорингом: {len(rows)}")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS transport_hexes (
            id SERIAL PRIMARY KEY, lat DOUBLE PRECISION, lon DOUBLE PRECISION,
            score REAL, dist_lrt REAL, dist_bus REAL
        )
    """)
    cur.execute("ALTER TABLE transport_hexes ADD COLUMN IF NOT EXISTS dist_road REAL")
    cur.execute("ALTER TABLE transport_hexes ADD COLUMN IF NOT EXISTS dist_junction REAL")
    cur.execute("ALTER TABLE transport_hexes ADD COLUMN IF NOT EXISTS route_count INTEGER")
    cur.execute("DELETE FROM transport_hexes")
    cur.executemany(
        "INSERT INTO transport_hexes (lat, lon, score, dist_lrt, dist_bus, dist_road, dist_junction, route_count) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        rows)
    db.commit()
    db.close()
    print("готово")


if __name__ == "__main__":
    asyncio.run(main())
