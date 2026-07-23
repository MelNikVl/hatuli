"""
Импорт дорог Астаны (кол-во полос) из OpenStreetMap в таблицу city_roads —
основа для «предварительной карты шума»: чем больше полос у дороги рядом
с гексом, тем выше шумовая нагрузка. Реальных данных о шуме (замеры) нет,
поэтому это прокси-оценка, а не измеренный уровень.

Полосы: берём тег lanes, если есть; если нет — грубый дефолт по типу
дороги (magistral/trunk обычно 4+, primary 3, secondary/tertiary 2) —
предварительная оценка, ТЗ явно допускает приближение.

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
[out:json][timeout:120];
area["name"~"Астана|Nur-Sultan"]["boundary"="administrative"]["admin_level"~"2|4"]->.a;
(
  way(area.a)[highway~"^(motorway|trunk|primary|secondary|tertiary)$"];
);
out tags center 8000;
"""

_DEFAULT_LANES = {
    "motorway": 4, "trunk": 4, "primary": 3,
    "secondary": 2, "tertiary": 2,
}


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
        center = el.get("center")
        if not center:
            continue
        lat, lon = round(center["lat"], 5), round(center["lon"], 5)
        key = (lat, lon, hwy)
        if key in seen:
            continue
        seen.add(key)
        lanes_raw = tags.get("lanes")
        try:
            lanes = int(str(lanes_raw).split(";")[0]) if lanes_raw else _DEFAULT_LANES[hwy]
        except ValueError:
            lanes = _DEFAULT_LANES[hwy]
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
