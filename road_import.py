"""
Импорт дорог Астаны (кол-во полос) из OpenStreetMap в таблицу city_roads —
основа для «предварительной карты шума»: чем больше полос у дороги рядом
с гексом, тем выше шумовая нагрузка. Реальных данных о шуме (замеры) нет,
поэтому это прокси-оценка, а не измеренный уровень.

Полосы: берём тег lanes, если есть; если нет — грубый дефолт по типу
дороги (magistral/trunk обычно 4+, primary 3, secondary/tertiary 2) —
предварительная оценка, ТЗ явно допускает приближение.

БАГ (найден и исправлен): раньше запрос брал `out tags center` — ОДНУ
точку (геометрический центр) на весь `way`. Длинная магистраль в OSM —
это один `way` на несколько километров, так что вся эта длина отдавала
ровно одну точку в city_roads; тепловая карта шума (гекс 30м + 1 кольцо
соседей, см. drawRoadHeat в dashboard.html) в итоге "видела" дорогу только
в одном гексе рядом с этим центром — отсюда дыры вдоль всей остальной
магистрали. Фикс: `out geom` отдаёт полную геометрию (все узлы way), и
мы сами семплируем точку через каждые ROAD_SAMPLE_STEP_M метров вдоль
полилинии — так дорога получает точки в каждом гексе по всей длине.

Запуск:
    venv/bin/python road_import.py --test    # показать, не записывая
    venv/bin/python road_import.py           # записать в city_roads

Данные меняются редко — перезапускать раз в несколько месяцев вручную.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

import httpx

sys.path.insert(0, ".")
from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("road_import")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

OVERPASS_MIRRORS = [
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

_QUERY = """
[out:json][timeout:180];
area["name"~"Астана|Nur-Sultan"]["boundary"="administrative"]["admin_level"~"2|4"]->.a;
(
  way(area.a)[highway~"^(motorway|trunk|primary|secondary|tertiary)$"];
);
out geom;
"""

_DEFAULT_LANES = {
    "motorway": 4, "trunk": 4, "primary": 3,
    "secondary": 2, "tertiary": 2,
}

# Шаг семплирования вдоль полилинии дороги — тот же порядок величин, что и
# гекс шумовой карты (NOISE_EDGE_M=30м в dashboard.html), чтобы почти каждый
# гекс вдоль дороги получил хотя бы одну точку.
ROAD_SAMPLE_STEP_M = 30.0
_M_PER_DEG_LAT = 110_574.0


def _m_per_deg_lon(lat: float) -> float:
    import math
    return 111_320.0 * math.cos(math.radians(lat))


def _sample_polyline(points: list[tuple[float, float]], step_m: float) -> list[tuple[float, float]]:
    """Точки через каждые step_m метров вдоль всей полилинии (lat, lon),
    независимо от длины отдельных сегментов way — так длинная магистраль
    получает точку в каждом гексе шумовой сетки, а не только в своих узлах."""
    if len(points) <= 1:
        return list(points)
    cum = [0.0]
    for (lat1, lon1), (lat2, lon2) in zip(points, points[1:]):
        mlon = _m_per_deg_lon((lat1 + lat2) / 2)
        dx = (lon2 - lon1) * mlon
        dy = (lat2 - lat1) * _M_PER_DEG_LAT
        cum.append(cum[-1] + (dx * dx + dy * dy) ** 0.5)
    total = cum[-1]
    if total <= 0:
        return [points[0]]
    out, seg_i, d = [], 0, 0.0
    while d <= total:
        while seg_i < len(cum) - 2 and cum[seg_i + 1] < d:
            seg_i += 1
        seg_len = cum[seg_i + 1] - cum[seg_i]
        f = (d - cum[seg_i]) / seg_len if seg_len > 0 else 0.0
        lat1, lon1 = points[seg_i]
        lat2, lon2 = points[seg_i + 1]
        out.append((lat1 + (lat2 - lat1) * f, lon1 + (lon2 - lon1) * f))
        d += step_m
    if out[-1] != points[-1]:
        out.append(points[-1])
    return out


async def fetch_roads() -> list[dict]:
    data = None
    last_error = None
    async with httpx.AsyncClient(timeout=150.0) as client:
        for mirror in OVERPASS_MIRRORS:
            try:
                log.info("пробую зеркало: %s", mirror)
                resp = await client.post(mirror, data={"data": _QUERY})
                if resp.status_code == 200:
                    data = resp.json()
                    log.info("успех: %s", mirror)
                    break
                log.warning("%s -> HTTP %s", mirror, resp.status_code)
                last_error = f"HTTP {resp.status_code}"
            except Exception as exc:
                log.warning("%s -> ошибка: %s", mirror, exc)
                last_error = str(exc)
    if data is None:
        raise RuntimeError(f"Все зеркала Overpass недоступны. Последняя ошибка: {last_error}")

    out, seen = [], set()
    for el in data.get("elements", []):
        tags = el.get("tags") or {}
        hwy = tags.get("highway")
        if hwy not in _DEFAULT_LANES:
            continue
        geom = el.get("geometry")
        if not geom:
            continue
        lanes_raw = tags.get("lanes")
        try:
            lanes = int(str(lanes_raw).split(";")[0]) if lanes_raw else _DEFAULT_LANES[hwy]
        except ValueError:
            lanes = _DEFAULT_LANES[hwy]
        points = [(p["lat"], p["lon"]) for p in geom if p.get("lat") is not None]
        for lat_f, lon_f in _sample_polyline(points, ROAD_SAMPLE_STEP_M):
            lat, lon = round(lat_f, 5), round(lon_f, 5)
            key = (lat, lon, hwy)
            if key in seen:
                continue
            seen.add(key)
            out.append({"lat": lat, "lon": lon, "lanes": lanes, "highway": hwy})
    return out


async def save(roads: list[dict]) -> None:
    from bot.db.pg import init_pool, execute
    await init_pool(DATABASE_URL)
    await execute("""
        CREATE TABLE IF NOT EXISTS city_roads (
            id SERIAL PRIMARY KEY,
            lat DOUBLE PRECISION NOT NULL,
            lon DOUBLE PRECISION NOT NULL,
            lanes INT NOT NULL,
            highway TEXT NOT NULL,
            updated_at TIMESTAMPTZ DEFAULT now(),
            UNIQUE(lat, lon, highway)
        )
    """)
    saved = 0
    for r in roads:
        await execute("""
            INSERT INTO city_roads (lat, lon, lanes, highway, updated_at)
            VALUES ($1, $2, $3, $4, now())
            ON CONFLICT (lat, lon, highway) DO UPDATE
              SET lanes = EXCLUDED.lanes, updated_at = now()
        """, r["lat"], r["lon"], r["lanes"], r["highway"])
        saved += 1
    log.info("Записано/обновлено: %d участков дорог", saved)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true")
    args = parser.parse_args()

    roads = await fetch_roads()
    by_hwy = {}
    for r in roads:
        by_hwy[r["highway"]] = by_hwy.get(r["highway"], 0) + 1
    log.info("Собрано из OSM: %s (всего %d)",
              ", ".join(f"{k}: {v}" for k, v in sorted(by_hwy.items())), len(roads))

    if args.test:
        for r in roads[:20]:
            log.info("  [%-10s] полос=%d  %.5f,%.5f", r["highway"], r["lanes"], r["lat"], r["lon"])
        log.info("--test: в БД НЕ записано.")
        return
    await save(roads)


if __name__ == "__main__":
    asyncio.run(main())
