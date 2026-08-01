#!/usr/bin/env python3
"""Тепловая карта транспортной доступности Астаны.
Гексы 100 м (ребро). Скоринг: ЛРТ-станции в радиусе 1 км (вес 0.75) +
автобусные остановки в радиусе 500 м (вес 0.4) + крупные дороги в радиусе
600 м (вес 0.3, доступность на машине) + развязки/перекрёстки в радиусе
800 м (вес 0.2, узел транспортной сети). Пишет transport_hexes.

Дороги/развязки берутся из OSM Overpass (см. bot/score_layers/osm.py —
тот же принцип зеркал-фолбэков, но здесь один большой запрос на весь
bbox города разом, а не поштучно на кэш-ячейку, т.к. это офлайн-скрипт,
а не хук на каждое объявление)."""
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

    lrt_c = np.clip(1 - dist_lrt / LRT_RADIUS, 0, 1)
    bus_c = np.clip(1 - dist_bus / BUS_RADIUS, 0, 1)
    road_c = np.clip(1 - dist_road / ROAD_RADIUS, 0, 1)
    junction_c = np.clip(1 - dist_junction / JUNCTION_RADIUS, 0, 1)
    score = np.clip(lrt_c * 0.75 + bus_c * 0.4 + road_c * 0.3 + junction_c * 0.2, 0, 1)

    mask = score > 0.05
    rows = [(float(la), float(lo), float(s), float(dl), float(db), float(dr), float(dj))
            for (la, lo), s, dl, db, dr, dj in zip(
                centers[mask], score[mask], dist_lrt[mask], dist_bus[mask],
                dist_road[mask], dist_junction[mask])]
    print(f"со скорингом: {len(rows)}")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS transport_hexes (
            id SERIAL PRIMARY KEY, lat DOUBLE PRECISION, lon DOUBLE PRECISION,
            score REAL, dist_lrt REAL, dist_bus REAL
        )
    """)
    cur.execute("ALTER TABLE transport_hexes ADD COLUMN IF NOT EXISTS dist_road REAL")
    cur.execute("ALTER TABLE transport_hexes ADD COLUMN IF NOT EXISTS dist_junction REAL")
    cur.execute("DELETE FROM transport_hexes")
    cur.executemany(
        "INSERT INTO transport_hexes (lat, lon, score, dist_lrt, dist_bus, dist_road, dist_junction) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s)",
        rows)
    db.commit()
    db.close()
    print("готово")


if __name__ == "__main__":
    asyncio.run(main())
