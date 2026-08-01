"""
Импорт ВСЕХ школ, детских садов и вузов Астаны из OpenStreetMap в таблицу
city_poi — одним массовым запросом Overpass по границе города (не радиусами
вокруг объявлений, как в слоях скоринга).

Почему OSM, а не 2GIS/Яндекс: у OSM данные бесплатны, легальны (лицензия
ODbL) и уже с координатами. 2GIS/Яндекс — лучшее покрытие, но API платные,
а скрапинг запрещён их условиями. Официальные реестры data.egov.kz — без
координат (только адреса), пригодятся позже для сверки полноты и рейтингов.

Запуск:
    venv/bin/python poi_import.py --test    # показать, не записывая
    venv/bin/python poi_import.py           # записать в city_poi

Данные меняются редко — достаточно перезапускать раз в месяц-два руками
или повесить на krisha-market цикл позже.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys

import httpx

sys.path.insert(0, ".")
from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("poi_import")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

# Несколько независимых зеркал Overpass — если основное недоступно
# (перегрузка/бан IP/сетевые проблемы конкретного региона), пробуем
# по очереди остальные вместо единой точки отказа.
OVERPASS_MIRRORS = [
    # maps.mail.ru — первым: единственное зеркало, доступное с сервера
    # (проверено 14.07.2026: остальные — connection refused либо таймаут,
    # похоже на блокировку маршрутов до европейских хостеров). Остальные
    # оставлены как фолбэк на случай, если доступность изменится.
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

# Один запрос: границы Астаны как area, внутри — школы/садики/вузы
_QUERY = """
[out:json][timeout:90];
area["name"~"Астана|Nur-Sultan"]["boundary"="administrative"]["admin_level"~"2|4"]->.a;
(
  node(area.a)[amenity=school];
  way(area.a)[amenity=school];
  node(area.a)[amenity=kindergarten];
  way(area.a)[amenity=kindergarten];
  node(area.a)[amenity=university];
  way(area.a)[amenity=university];
);
out tags center 3000;
"""


def _addr(tags: dict) -> str | None:
    street = tags.get("addr:street")
    house = tags.get("addr:housenumber")
    if street and house:
        return f"{street}, {house}"
    return street or None


async def fetch_poi() -> list[dict]:
    data = None
    last_error = None
    async with httpx.AsyncClient(timeout=120.0) as client:
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
        raise RuntimeError(
            f"Все зеркала Overpass недоступны. Последняя ошибка: {last_error}. "
            f"Проверь сеть с сервера: curl -v --max-time 10 {OVERPASS_MIRRORS[0]}"
        )

    out, seen = [], set()
    for el in data.get("elements", []):
        tags = el.get("tags") or {}
        kind = tags.get("amenity")
        if kind not in ("school", "kindergarten", "university"):
            continue
        if "lat" in el:
            lat, lon = el["lat"], el["lon"]
        elif el.get("center"):
            lat, lon = el["center"]["lat"], el["center"]["lon"]
        else:
            continue
        key = (kind, round(lat, 5), round(lon, 5))
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "kind": kind,
            "name": tags.get("name") or tags.get("name:ru") or tags.get("name:kk"),
            "lat": lat, "lon": lon,
            "address": _addr(tags),
            "extra": {k: v for k, v in tags.items()
                      if k in ("operator", "operator:type", "capacity",
                               "isced:level", "phone", "website")},
        })
    return out


async def save(poi: list[dict]) -> None:
    from bot.db.pg import init_pool, execute
    await init_pool(DATABASE_URL)
    saved = 0
    for p in poi:
        await execute("""
            INSERT INTO city_poi (kind, name, lat, lon, address, extra, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, now())
            ON CONFLICT (kind, lat, lon) DO UPDATE
              SET name = EXCLUDED.name, address = EXCLUDED.address,
                  extra = EXCLUDED.extra, updated_at = now()
        """, p["kind"], p["name"], p["lat"], p["lon"], p["address"],
             json.dumps(p["extra"], ensure_ascii=False))
        saved += 1
    log.info("Записано/обновлено: %d POI", saved)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true")
    args = parser.parse_args()

    poi = await fetch_poi()
    by_kind = {}
    for p in poi:
        by_kind[p["kind"]] = by_kind.get(p["kind"], 0) + 1
    named = sum(1 for p in poi if p["name"])
    log.info("Собрано из OSM: %s (с названием: %d из %d)",
             ", ".join(f"{k}: {v}" for k, v in sorted(by_kind.items())), named, len(poi))

    if args.test:
        for p in poi[:20]:
            log.info("  [%-12s] %-45s %.5f,%.5f %s",
                     p["kind"], (p["name"] or "без названия")[:45],
                     p["lat"], p["lon"], p["address"] or "")
        log.info("--test: в БД НЕ записано.")
        return
    await save(poi)


if __name__ == "__main__":
    asyncio.run(main())
