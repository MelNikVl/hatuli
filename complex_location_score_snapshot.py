#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ежемесячный пересчёт complex_location_scores (Фаза L1 продуктового
трека «Локация», docs/location_product_design.md §7, задача 2026-08-14,
миграция 072) — п.9 «Часть 3» scoring_roadmap.md.

Читает bot/core/location_score.py::compute_complex_location_score() КАК
ЕСТЬ (не меняет её и не трогает живой /admin/api/complex/{id}/location-
score, см. РЕШЕНИЕ 2 плана L1 — тот отдаёт сырой total/factors/
confidence, единственный консьюмер complex_detail.html:645). Этот
скрипт — отдельный слой: нормализует factors в 0-100 (bot/core/location_
score.py::normalize_group_weighted() — взвешенное среднее по пяти
latent-свойствам локации, задача 2026-08-15 "Location Reliability
Phase" v2, раньше был линейный _TOTAL_ADJ_MIN/MAX по total, убран),
группирует факторы в breakdown (transport/infra/environment/risk/
urban_quality + informational — environment = бывшие "шум"+"зелень"
объединены, см. location_score.py про переименование), пишет
append-only снимок.

**noise_score/green_score колонки СОХРАНЕНЫ** (задача 2026-08-15 v2 не
тянет миграцию/переделку UI ради переименования групп) — считаются явно
по факторам noise/parks напрямую, не через _GROUPS (тех ключей там
больше нет, см. _process_one() ниже).

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

# Группировка факторов — задача 2026-08-15 ("Location Reliability Phase")
# сделала bot/core/location_score.py каноническим источником: _GROUPS/
# _INFORMATIONAL/веса групп теперь живут ТАМ (нужны и для normalize_
# group_weighted(), не только для breakdown/UI здесь) — импортируем, не
# дублируем. РЕШЕНИЕ 4 плана L1 (building_age -> риски, bank -> вне
# групп, informational-only) по-прежнему в силе, отражено в исходном
# определении.
from bot.core.location_score import _GROUPS, _INFORMATIONAL

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
    from bot.core.location_score import _group_pct, _group_confidence
    breakdown = {group: {k: factors[k] for k in keys if k in factors}
                 for group, keys in _GROUPS.items()}
    breakdown["informational"] = {k: factors[k] for k in _INFORMATIONAL if k in factors}
    # _group_scores — задача 2026-08-15 v2, коммит "Confidence": пара
    # "score X/100, confidence Y%" на КАЖДОЕ из пяти latent-свойств (не
    # только общий confidence всей локации, см. result["confidence"] в
    # _process_one() ниже). urban_quality со СЕЙЧАС пустой схемой факторов
    # даёт confidence=0 структурно, всегда.
    breakdown["_group_scores"] = {
        g: {"score": round(_group_pct(g, factors)), "confidence": _group_confidence(g, factors)}
        for g in _GROUPS
    }
    return breakdown


def _normalize_score(factors: dict) -> int:
    """Тонкая обёртка над location_score.normalize_group_weighted() —
    задача 2026-08-15 ("Location Reliability Phase", коммит "Семантика +
    групповая модель"). РАНЬШЕ принимала total (int, Σ всех adj) и
    нормализовала по единому _TOTAL_ADJ_MIN/MAX (убраны из location_
    score.py) — теперь групповая модель, принимает весь factors целиком
    (нужны отдельные суммы по группам, не общий total)."""
    from bot.core.location_score import normalize_group_weighted
    return normalize_group_weighted(factors)


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
            r["id"], _normalize_score(factors), result["confidence"],
            _group_sum(factors, _GROUPS["transport"]), _group_sum(factors, _GROUPS["infra"]),
            # noise_score/green_score — задача 2026-08-15 v2 ("Семантика +
            # якоря + иерархическая модель"): "noise"/"green" больше НЕ
            # отдельные ключи _GROUPS (объединены в "environment", см.
            # location_score.py) — колонки СОХРАНЕНЫ как есть (не тянем
            # миграцию/переделку UI ради одного захода), считаем явными
            # tuple вместо _GROUPS[...] — те же самые факторы, что раньше.
            _group_sum(factors, ("noise",)), _group_sum(factors, ("parks",)),
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
