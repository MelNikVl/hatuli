"""
Общий помощник слоёв: OSM Overpass с кешем в PostgreSQL.

Координаты округляются до 3 знаков (~110 м сетка) — один запрос к Overpass
на ячейку, дальше ответ берётся из кеша (osm_cache) 60 дней.
Overpass бесплатный, но просит вежливости: не дёргаем чаще необходимого.
"""
from __future__ import annotations

import json
import logging

import httpx

from bot.db.pg import execute, fetchrow

logger = logging.getLogger(__name__)

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
CACHE_DAYS = 60


def grid(v: float) -> float:
    return round(v, 3)


async def overpass_cached(lat: float, lon: float, kind: str, query: str) -> dict | None:
    """Запрос к Overpass с кешем. query — готовый Overpass QL."""
    glat, glon = grid(lat), grid(lon)
    try:
        row = await fetchrow(
            "SELECT payload FROM osm_cache WHERE grid_lat=$1 AND grid_lon=$2 AND kind=$3 "
            "AND fetched_at > now() - ($4 || ' days')::interval",
            glat, glon, kind, str(CACHE_DAYS),
        )
        if row:
            payload = row["payload"]
            return json.loads(payload) if isinstance(payload, str) else payload
    except Exception as exc:
        logger.warning("osm_cache read failed: %s", exc)

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(OVERPASS_URL, data={"data": query})
        if resp.status_code != 200:
            logger.warning("overpass %s -> %s", kind, resp.status_code)
            return None
        data = resp.json()
    except Exception as exc:
        logger.warning("overpass %s failed: %s", kind, exc)
        return None

    try:
        await execute(
            """INSERT INTO osm_cache (grid_lat, grid_lon, kind, payload, fetched_at)
               VALUES ($1,$2,$3,$4::jsonb, now())
               ON CONFLICT (grid_lat, grid_lon, kind)
               DO UPDATE SET payload=$4::jsonb, fetched_at=now()""",
            glat, glon, kind, json.dumps(data),
        )
    except Exception as exc:
        logger.warning("osm_cache write failed: %s", exc)
    return data


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Расстояние между точками в метрах."""
    import math
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def element_coords(el: dict) -> tuple[float, float] | None:
    """Координаты элемента Overpass (node → lat/lon, way → center)."""
    if "lat" in el and "lon" in el:
        return el["lat"], el["lon"]
    c = el.get("center")
    if c:
        return c.get("lat"), c.get("lon")
    return None
