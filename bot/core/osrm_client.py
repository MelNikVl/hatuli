"""
Клиент self-hosted OSRM (foot-профиль) — реальные пешеходные маршруты
вместо прямой линии (Фаза L3 продуктового трека «Локация»,
docs/location_product_design.md §3/§4, задача 2026-08-15, миграция 075).

Сервер: контейнер osrm-foot на 127.0.0.1:5000 (граф = OSM-экстракт
Казахстана, /home/nik/osrm, пересборка ~раз в квартал). Переопределяется
env OSRM_URL.

Ключевая идея — Table API вместо point-to-point /route: на один ЖК делаем
ONE-TO-MANY запрос (origin × K кандидатов, найденных дешёвым SQL
bbox-префильтром по хаверсину) и берём min по walking distance. Матрица
1×8 считается локально за миллисекунды — полный прогон ~2350 ЖК × 5
типов назначений укладывается в десятки минут, а не в сутки /route.

Деградация мягкая (тот же принцип, что score_layers/osm.py для
Overpass): при недоступности OSRM функции возвращают None /
no_route_reason='osrm_unavailable', вызывающий код фолбэкается на
хаверсин (Unknown ≠ average, docs/verdict_strategy.md §3.1).
"""
from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

OSRM_URL = os.getenv("OSRM_URL", "http://127.0.0.1:5000")

# ratio = walking/haversine выше этого порога — вероятный физический
# барьер между ЖК и POI (река/трасса/забор): маршрут вдвое+ длиннее
# прямой. Порог из задачи 2026-08-15; пишется в complex_walkability.
# barrier писателем снапшота, не переинтерпретируется при чтении.
BARRIER_RATIO = 1.5

# Сколько ближайших по прямой кандидатов отдаём в Table-запрос. 8 с
# запасом покрывает случай "ближайший по прямой за рекой, реально
# ближайший — третий по хаверсину"; матрица 1×8 для OSRM бесплатна.
DEFAULT_CANDIDATES = 8


async def walking_table(client: httpx.AsyncClient, origin: tuple[float, float],
                        candidates: list[dict]) -> list[dict] | None:
    """GET /table/v1/foot/{origin};{candidates}?sources=0 — реальные
    пешеходные distance/duration от origin (lat, lon) до каждого
    кандидата (dict с ключами lat/lon).

    Возвращает candidates, обогащённые walking_distance_m /
    walking_duration_s (None для конкретной пары = маршрута нет, например
    POI в пешеходно-недостижимой компоненте графа). None целиком — OSRM
    недоступен/ошибка запроса; если сам origin не снапнулся к графу,
    у всех кандидатов дополнительно проставляется no_route_reason
    ='no_snap' (иначе 'no_route' у пар без маршрута).
    """
    coords = ";".join(
        [f"{origin[1]},{origin[0]}"]
        + [f"{c['lon']},{c['lat']}" for c in candidates]
    )
    # annotations=duration,distance — порядок важен: OSRM с "distance,
    # duration" молча возвращает ТОЛЬКО distances (проверено живьём на
    # osrm-backend latest 2026-08-15), с "duration,distance" — оба поля.
    url = (f"{OSRM_URL}/table/v1/foot/{coords}"
           f"?sources=0&annotations=duration,distance")
    try:
        resp = await client.get(url)
        data = resp.json()
    except Exception as exc:
        logger.warning("osrm table failed: %s", exc)
        return None
    if resp.status_code != 200 or data.get("code") != "Ok":
        # NoSegment — origin или кандидат не снапнулся к foot-графу
        # (новостройка, забор ЖК, дыра в OSM). Разметить все пары, чтобы
        # вызывающий код записал no_route_reason, а не молчал.
        reason = "no_snap" if data.get("code") == "NoSegment" else "no_route"
        logger.warning("osrm table %s: %s", data.get("code"), data.get("message"))
        return [{**c, "walking_distance_m": None, "walking_duration_s": None,
                 "no_route_reason": reason} for c in candidates]

    distances = (data.get("distances") or [[]])[0]
    durations = (data.get("durations") or [[]])[0]
    out = []
    for i, c in enumerate(candidates):
        # +1: distances[0] — сам origin (sources=0, destinations=все)
        d = distances[i + 1] if i + 1 < len(distances) else None
        t = durations[i + 1] if i + 1 < len(durations) else None
        out.append({**c,
                    "walking_distance_m": d,
                    "walking_duration_s": t,
                    "no_route_reason": None if d is not None else "no_route"})
    return out


async def nearest_walking(client: httpx.AsyncClient, origin: tuple[float, float],
                          candidates: list[dict]) -> dict | None:
    """Ближайший кандидат ПО ФАКТУ пешеходного маршрута (не по прямой —
    в этом смысл: ближайший по хаверсину может быть за рекой).

    Возвращает dict: лучший кандидат + walking_distance_m /
    walking_duration_s / haversine_distance_m / ratio / barrier.
    None — OSRM недоступен; если маршрутов нет ни до одного кандидата,
    возвращается dict с walking_* = None и no_route_reason ближайшего
    по прямой кандидата (строка в complex_walkability всё равно пишется —
    «попытались, вот что знаем»).
    """
    from bot.core.geo import haversine_km

    enriched = await walking_table(client, origin, candidates)
    if enriched is None:
        return None

    for c in enriched:
        c["haversine_distance_m"] = haversine_km(
            origin[0], origin[1], c["lat"], c["lon"]) * 1000

    routable = [c for c in enriched if c["walking_distance_m"] is not None]
    if not routable:
        best = min(enriched, key=lambda c: c["haversine_distance_m"])
        return {**best, "ratio": None, "barrier": None}

    best = min(routable, key=lambda c: c["walking_distance_m"])
    hav = best["haversine_distance_m"]
    ratio = best["walking_distance_m"] / hav if hav > 1.0 else 1.0
    return {**best, "ratio": ratio, "barrier": ratio > BARRIER_RATIO}


async def check_osrm(timeout: float = 5.0) -> bool:
    """Healthcheck OSRM (паттерн check_mirrors() в score_layers/osm.py):
    жив ли сервис и загружен ли foot-граф. Используется снапшотом перед
    прогоном, чтобы не писать 2350×5 строк 'osrm_unavailable' при лежачем
    контейнере."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{OSRM_URL}/route/v1/foot/71.43,51.13;71.44,51.13")
            return resp.status_code == 200 and resp.json().get("code") == "Ok"
    except Exception as exc:
        logger.warning("osrm healthcheck failed: %s", exc)
        return False
