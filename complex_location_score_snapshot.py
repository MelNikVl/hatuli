#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ежемесячный пересчёт complex_location_scores (Фаза L1 продуктового
трека «Локация», docs/location_product_design.md §7, задача 2026-08-14,
миграция 072) — п.9 «Часть 3» scoring_roadmap.md.

Читает bot/core/location_score.py::compute_complex_location_score() КАК
ЕСТЬ (не меняет её и не трогает живой /admin/api/complex/{id}/location-
score, см. РЕШЕНИЕ 2 плана L1 — тот отдаёт сырой total/factors/
confidence, единственный консьюмер complex_detail.html:645). Этот
скрипт — отдельный слой: нормализует total в 0-100 (bot/core/location_
score.py::_TOTAL_ADJ_MIN/_TOTAL_ADJ_MAX), группирует факторы в breakdown
(транспорт/инфраструктура/шум/зелень/риски + informational), пишет
append-only снимок.

**НИЗКИЙ confidence — валидная строка, НЕ повод пропустить ЖК.** Полный
отказ Overpass при пустом osm_cache всё равно даёт transport_hexes/
demolition_houses/building_age/bank факторы (не зависят от Overpass) —
compute_complex_location_score() и так это отражает через свой
confidence, эта функция просто переносит его как есть. Unknown ≠
average (docs/verdict_strategy.md §3.1) — "попытались, вот что реально
знаем" фиксируется строкой с низким confidence, а не тишиной.
Единственная причина ПРОПУСТИТЬ ЖК — отсутствие резолвящихся координат
вовсе (resolve_complex_geo_centroid() вернула None).

Расписание: krisha-complex-location-score.timer (ежемесячно, 1 число).
Разовая проверка: venv/bin/python complex_location_score_snapshot.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.FileHandler("complex_location_score_snapshot.log", encoding="utf-8", errors="replace")],
)
log = logging.getLogger("complex_location_score_snapshot")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

SCORE_VERSION = "loc_v1"

# РЕШЕНИЕ 4 плана L1: building_age -> риски (долгосрочный риск
# обслуживания), bank -> вне групп (всегда adj=0, живёт только в
# informational, не считается ни в одну group-сумму).
_GROUPS: dict[str, tuple[str, ...]] = {
    "transport": ("transit_stops", "lrt_access", "road_access", "route_connectivity"),
    "infra": ("schools", "amenities"),
    "noise": ("noise",),
    "green": ("parks",),
    "risk": ("demolition", "building_age"),
}
_INFORMATIONAL: tuple[str, ...] = ("bank",)

# Сдвиг центроида крупнее этого — логируется как "координаты сместились"
# (информационно, см. план L1 п.5 "Задача 4" — своего event-триггера на
# пересчёт нет, месячный полный прогон и так покрывает дрейф координат
# в пределах месяца).
_DRIFT_ALERT_M = 100.0

# Ограничение параллелизма по ЖК — живая находка при разработке: строго
# последовательный проход (1 ЖК за раз) на холодном osm_cache уходил в
# многоминутные простои на каскадах "все 4 зеркала Overpass недоступны"
# (bot/score_layers/osm.py — на сервере реально жив 1-2 из 4), 6
# complexes за первые ~5 минут прогона — непрактично для ~2000+ ЖК.
# Небольшая ограниченная конкурентность (не asyncio.gather БЕЗ лимита —
# это перегрузило бы и так нестабильный публичный Overpass ещё
# сильнее) — компромисс между "не долбить бесплатный сервис" и
# "закончить прогон за разумное время".
_CONCURRENCY = 5


def _group_sum(factors: dict, keys: tuple[str, ...]) -> int:
    return sum(int(factors[k]["adj"]) for k in keys if k in factors)


def _build_breakdown(factors: dict) -> dict:
    breakdown = {group: {k: factors[k] for k in keys if k in factors}
                 for group, keys in _GROUPS.items()}
    breakdown["informational"] = {k: factors[k] for k in _INFORMATIONAL if k in factors}
    return breakdown


def _normalize_score(total: int) -> int:
    from bot.core.location_score import _TOTAL_ADJ_MIN, _TOTAL_ADJ_MAX
    clamped = max(_TOTAL_ADJ_MIN, min(_TOTAL_ADJ_MAX, total))
    return round(100 * (clamped - _TOTAL_ADJ_MIN) / (_TOTAL_ADJ_MAX - _TOTAL_ADJ_MIN))


async def _process_one(r: dict, commit: str, sem: asyncio.Semaphore) -> str:
    """Возвращает 'written' | 'no_coords' — статус ОДНОГО ЖК. Ограничено
    семафором снаружи (_CONCURRENCY) — не более N complexes одновременно
    бьют в Overpass/БД разом."""
    from bot.db.pg import fetchrow, execute
    from bot.core.house_resolution import resolve_complex_geo_centroid
    from bot.core.location_score import compute_complex_location_score

    async with sem:
        centroid = await resolve_complex_geo_centroid(r["id"], r["name"])
        if centroid is None:
            return "no_coords"
        lat, lon = centroid

        result = await compute_complex_location_score(
            lat, lon, year_built=r["year_built"], district=r["district"])
        if result is None:
            # Теоретически недостижимо (centroid уже не None -> lat/lon
            # заданы) — защитный случай, не молчим.
            log.warning("compute_complex_location_score вернула None при заданных координатах, complex_id=%s", r["id"])
            return "no_coords"

        factors = result["factors"]
        breakdown = _build_breakdown(factors)

        prev = await fetchrow("""
            SELECT lat, lon FROM complex_location_scores
            WHERE complex_id=$1 ORDER BY computed_at DESC LIMIT 1
        """, r["id"])
        drifted = False
        if prev and prev["lat"] is not None and prev["lon"] is not None:
            from bot.core.geo import haversine_km
            dist_m = haversine_km(float(prev["lat"]), float(prev["lon"]), lat, lon) * 1000
            if dist_m > _DRIFT_ALERT_M:
                drifted = True
                log.info("complex_id=%s: координаты сместились на %.0fм с прошлого снимка", r["id"], dist_m)

        await execute("""
            INSERT INTO complex_location_scores (
                complex_id, score, confidence, transport_score, infra_score,
                noise_score, green_score, risk_score, lat, lon, breakdown,
                score_version, git_commit
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, $12, $13)
        """,
            r["id"], _normalize_score(result["total"]), result["confidence"],
            _group_sum(factors, _GROUPS["transport"]), _group_sum(factors, _GROUPS["infra"]),
            _group_sum(factors, _GROUPS["noise"]), _group_sum(factors, _GROUPS["green"]),
            _group_sum(factors, _GROUPS["risk"]), lat, lon,
            json.dumps(breakdown, ensure_ascii=False), SCORE_VERSION, commit,
        )
        return "written_drifted" if drifted else "written"


async def run_snapshot(complex_ids: list[int] | None = None) -> dict:
    """complex_ids — опциональный скоуп ТОЛЬКО для дешёвых тестов (тот же
    паттерн, что deal_score_snapshot.py/hex_market_stats_snapshot.py) —
    прод-путь (None) всегда идёт по всем complexes с координатами."""
    from bot.db.pg import fetch
    from bot.git_info import git_hash

    if complex_ids is not None:
        rows = await fetch("""
            SELECT id, name, year_built, district FROM complexes
            WHERE COALESCE(is_garbage, FALSE) = FALSE AND COALESCE(is_street, FALSE) = FALSE
              AND id = ANY($1::int[])
        """, complex_ids)
    else:
        rows = await fetch("""
            SELECT id, name, year_built, district FROM complexes
            WHERE COALESCE(is_garbage, FALSE) = FALSE AND COALESCE(is_street, FALSE) = FALSE
        """)

    commit = git_hash()
    sem = asyncio.Semaphore(_CONCURRENCY)
    statuses = await asyncio.gather(*(_process_one(r, commit, sem) for r in rows))

    written = sum(1 for s in statuses if s.startswith("written"))
    drifted = sum(1 for s in statuses if s == "written_drifted")
    no_coords = sum(1 for s in statuses if s == "no_coords")

    log.info("complex_location_score_snapshot: written=%d no_coords=%d drifted=%d (из %d complexes в скоупе)",
              written, no_coords, drifted, len(rows))
    return {"written": written, "no_coords": no_coords, "drifted": drifted, "total": len(rows)}


async def main() -> None:
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    try:
        await run_snapshot()
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
