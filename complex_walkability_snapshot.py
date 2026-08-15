#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ежемесячный пересчёт complex_walkability (Фаза L3 продуктового трека
«Локация», docs/location_product_design.md §3/§4 «walkability», задача
2026-08-15, миграция 075) — реальные пешеходные маршруты до ближайших
POI вместо прямой линии (haversine), роутинг через self-hosted OSRM
(bot/core/osrm_client.py).

Для каждого ЖК × 5 типов назначений (school/kindergarten/transit/shop/
park): дешёвый SQL-префильтр топ-K=8 кандидатов по квадрату расстояния
(хаверсин-аппроксимация), затем ОДИН Table-запрос к OSRM (матрица 1×8) —
и min по walking distance. Ближайший по факту маршрута POI может
отличаться от ближайшего по прямой (река/трасса) — это и есть ценность
таблицы. ratio = walking/haversine > 1.5 -> barrier=TRUE (порог
osrm_client.BARRIER_RATIO).

**OSRM лежит — прогон НЕ начинается** (check_osrm() перед стартом):
не пишем 2350×5 строк 'osrm_unavailable' при лежачем контейнере,
просто логируем и выходим — таймер повторит через месяц, точечный
пересчёт доступен руками.

**Маршрута нет (no_snap/no_route) — валидная строка, НЕ повод пропустить
ЖК**: пишется walking=NULL + haversine + no_route_reason, скоринг
фолбэкается на прямую (Unknown ≠ average, docs/verdict_strategy.md
§3.1, тот же принцип, что complex_location_score_snapshot.py).

Источники назначений: astana_schools/astana_kindergartens (статичные
таблицы), transport_stops, city_poi kind='shop'/'park' (наполняются
poi_import.py — снапшот НЕ зависит от живого Overpass).

Расписание: krisha-complex-walkability.timer (ежемесячно, 1 число,
раньше krisha-complex-location-score.timer — чтобы тот видел свежие
walking-дистанции). Разовая проверка:
    venv/bin/python complex_walkability_snapshot.py --complex-ids 12,34
    venv/bin/python complex_walkability_snapshot.py
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.FileHandler("complex_walkability_snapshot.log", encoding="utf-8", errors="replace")],
)
log = logging.getLogger("complex_walkability_snapshot")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

# Каталог подготовленного OSM-экстракта (граф foot-профиля). Дата mtime
# pbf-файла идёт в engine_version — отличить строки до/после пересборки
# графа (тот же смысл, что score_version в complex_location_scores).
OSRM_DATA_DIR = Path(os.getenv("OSRM_DATA_DIR", "/home/nik/osrm"))
OSRM_PBF = OSRM_DATA_DIR / "kazakhstan-latest.osm.pbf"

ENGINE = "osrm-foot-v1"

# Локальный OSRM, не публичный Overpass — конкурентность шире, чем у
# complex_location_score_snapshot.py (_CONCURRENCY=5 там продиктована
# нестабильностью Overpass, здесь её нет). Матрица 1×8 — миллисекунды.
_CONCURRENCY = 8

# Сколько ближайших по прямой кандидатов отдаём в Table-запрос
# (osrm_client.DEFAULT_CANDIDATES). 8 с запасом покрывает «ближайший по
# прямой за рекой, реально ближайший — третий по хаверсину».
_K = 8

# Типы назначений -> (таблица/запрос). Bbox-префильтр не нужен: все
# таблицы маленькие (максимум несколько тысяч строк), полный скан с
# ORDER BY по квадрату расстояния и LIMIT 8 — копейки (тот же паттерн,
# что astana_schools в location_score.py::_schools_factor).
_SOURCES = {
    "school": "SELECT name, lat, lon FROM astana_schools WHERE lat IS NOT NULL AND lon IS NOT NULL",
    "kindergarten": "SELECT name, lat, lon FROM astana_kindergartens WHERE lat IS NOT NULL AND lon IS NOT NULL",
    "transit": "SELECT name, lat, lon FROM transport_stops WHERE lat IS NOT NULL AND lon IS NOT NULL",
    "shop": "SELECT name, lat, lon FROM city_poi WHERE kind='shop'",
    "park": "SELECT name, lat, lon FROM city_poi WHERE kind='park'",
}


def _engine_version() -> str:
    """'osrm-foot-v1@<дата pbf>' — дата OSM-экстракта из mtime файла,
    'unknown' если файла нет (граф собран иначе/вручную)."""
    try:
        mtime = OSRM_PBF.stat().st_mtime
        return f"{ENGINE}@{datetime.fromtimestamp(mtime, timezone.utc).date()}"
    except OSError:
        return f"{ENGINE}@unknown"


async def _load_candidates() -> dict[str, list[dict]]:
    """Все POI-назначения одним чтением на тип (таблицы маленькие) —
    дальше префильтр топ-K по каждому ЖК делается в Python, без
    2350×5 отдельных SQL-запросов."""
    from bot.db.pg import fetch
    out: dict[str, list[dict]] = {}
    for dtype, sql in _SOURCES.items():
        rows = await fetch(sql)
        out[dtype] = [{"name": r["name"], "lat": float(r["lat"]), "lon": float(r["lon"])}
                      for r in rows]
        log.info("кандидаты %s: %d", dtype, len(out[dtype]))
    return out


def _top_k(candidates: list[dict], lat: float, lon: float, k: int) -> list[dict]:
    """Топ-K по квадрату расстояния (дешёвая аппроксимация, точный
    хаверсин не нужен для префильтра — его посчитает nearest_walking)."""
    return sorted(candidates,
                  key=lambda c: (c["lat"] - lat) ** 2 + (c["lon"] - lon) ** 2)[:k]


async def _process_one(r: dict, candidates_by_type: dict[str, list[dict]],
                       client, commit: str, engine_version: str,
                       sem: asyncio.Semaphore) -> str:
    """Пишет 5 строк (по типу назначения) для ОДНОГО ЖК. Возвращает
    'written' | 'no_coords' | 'osrm_error' (частичные no_route внутри
    строк — не ошибка прогона, см. докстринг модуля)."""
    from bot.db.pg import execute
    from bot.core.house_resolution import resolve_complex_geo_centroid
    from bot.core.osrm_client import nearest_walking

    async with sem:
        centroid = await resolve_complex_geo_centroid(r["id"], r["name"])
        if centroid is None:
            return "no_coords"
        lat, lon = centroid

        had_error = False
        for dtype, candidates in candidates_by_type.items():
            if not candidates:
                had_error = True
                log.warning("complex_id=%s: нет кандидатов типа %s", r["id"], dtype)
                continue
            best = await nearest_walking(client, (lat, lon), _top_k(candidates, lat, lon, _K))
            if best is None:
                # OSRM отвалился посреди прогона — не пишем массово
                # osrm_unavailable (та же логика, что check_osrm() на
                # старте): прерываем ЖК целиком, статус уйдёт в сводку.
                had_error = True
                break
            await execute("""
                INSERT INTO complex_walkability (
                    complex_id, destination_type, walking_distance_m, walking_duration_s,
                    haversine_distance_m, ratio, barrier, dest_name, dest_lat, dest_lon,
                    complex_lat, complex_lon, no_route_reason, engine_version, git_commit
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
            """,
                r["id"], dtype, best["walking_distance_m"], best["walking_duration_s"],
                best["haversine_distance_m"], best["ratio"], best["barrier"],
                best["name"], best["lat"], best["lon"], lat, lon,
                best["no_route_reason"], engine_version, commit,
            )
        return "osrm_error" if had_error else "written"


async def run_snapshot(complex_ids: list[int] | None = None) -> dict:
    """complex_ids — опциональный скоуп ТОЛЬКО для дешёвых тестов (тот же
    паттерн, что complex_location_score_snapshot.py) — прод-путь (None)
    всегда идёт по всем complexes."""
    import httpx
    from bot.db.pg import fetch
    from bot.git_info import git_hash
    from bot.core.osrm_client import check_osrm

    if not await check_osrm():
        log.error("OSRM недоступен (см. bot/core/osrm_client.py, OSRM_URL) — "
                  "прогон отменён, строки НЕ писались. Проверь контейнер osrm-foot.")
        return {"written": 0, "no_coords": 0, "osrm_error": 0, "total": 0, "aborted": True}

    if complex_ids is not None:
        rows = await fetch("""
            SELECT id, name FROM complexes
            WHERE COALESCE(is_garbage, FALSE) = FALSE AND COALESCE(is_street, FALSE) = FALSE
              AND id = ANY($1::int[])
        """, complex_ids)
    else:
        rows = await fetch("""
            SELECT id, name FROM complexes
            WHERE COALESCE(is_garbage, FALSE) = FALSE AND COALESCE(is_street, FALSE) = FALSE
        """)

    candidates_by_type = await _load_candidates()
    commit = git_hash()
    engine_version = _engine_version()
    sem = asyncio.Semaphore(_CONCURRENCY)

    async with httpx.AsyncClient(timeout=30.0) as client:
        statuses = await asyncio.gather(*(
            _process_one(r, candidates_by_type, client, commit, engine_version, sem)
            for r in rows))

    written = sum(1 for s in statuses if s == "written")
    no_coords = sum(1 for s in statuses if s == "no_coords")
    osrm_error = sum(1 for s in statuses if s == "osrm_error")

    log.info("complex_walkability_snapshot: written=%d no_coords=%d osrm_error=%d (из %d complexes в скоупе)",
             written, no_coords, osrm_error, len(rows))
    return {"written": written, "no_coords": no_coords, "osrm_error": osrm_error,
            "total": len(rows), "aborted": False}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--complex-ids", default=None,
                        help="через запятую — точечный пересчёт (тесты/отладка), без флага — все ЖК")
    args = parser.parse_args()
    complex_ids = [int(x) for x in args.complex_ids.split(",")] if args.complex_ids else None

    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    try:
        result = await run_snapshot(complex_ids)
        print(result)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
