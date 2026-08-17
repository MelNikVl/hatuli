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


async def _process_one(r: dict, commit: str, sem: asyncio.Semaphore, dry_run: bool = False) -> str:
    """Возвращает 'written'/'written_drifted' | 'no_coords' — статус
    ОДНОГО ЖК. Ограничено семафором снаружи (_CONCURRENCY) — не более N
    complexes одновременно бьют в Overpass/БД разом.

    Поднимает исключение наверх, если что-то реально сломалось (Overpass/
    БД/парсинг) — caller (run_snapshot) ловит через asyncio.gather(...,
    return_exceptions=True) и считает это 'failed', НЕ роняя остальной
    batch (задача 2026-08-17, "ошибка одного ЖК не должна прекращать
    весь batch" — раньше gather() был БЕЗ return_exceptions=True, первое
    же исключение обрывало ВЕСЬ прогон, не только этот ЖК).

    dry_run — задача 2026-08-17, "по возможности --dry-run": Overpass/
    OSRM/БД читаются как обычно (это и есть "прогон" в смысле проверки),
    INSERT в complex_location_scores пропускается. Единственный INSERT
    здесь один, атомарный — ни в dry-run, ни при реальной записи не
    может получиться ЧАСТИЧНО записанная строка одного ЖК (задача,
    явно: "никаких частично записанных результатов одного ЖК")."""
    from bot.db.pg import fetchrow, execute
    from bot.core.house_resolution import resolve_complex_geo_centroid
    from bot.core.location_score import compute_complex_location_score

    async with sem:
        centroid = await resolve_complex_geo_centroid(r["id"], r["name"])
        if centroid is None:
            return "no_coords"
        lat, lon = centroid

        result = await compute_complex_location_score(
            lat, lon, year_built=r["year_built"], district=r["district"],
            complex_id=r["id"])
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

        if not dry_run:
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


async def run_snapshot(complex_ids: list[int] | None = None, limit: int | None = None,
                        dry_run: bool = False, only_missing: bool = False) -> dict:
    """complex_ids — опциональный скоуп (canary/тесты — тот же паттерн,
    что deal_score_snapshot.py/hex_market_stats_snapshot.py); limit —
    ограничить выборку сверху (задача 2026-08-17, canary-режим); без
    обоих — прод-путь, все complexes с координатами. dry_run — см.
    _process_one.

    only_missing (задача 2026-08-17, "завершение Location Score без
    повторного пересчёта уже свежих") — исключает complexes, у которых
    УЖЕ есть строка complex_location_scores с computed_at СЕГОДНЯШНИМ
    числом (не привязано к конкретному git_commit/score_version — код
    между прерванным утренним прогоном и этим дозапуском менялся
    (roads/parks в city_poi), но сам смысл "уже свежий результат
    СЕГОДНЯ" не должен тянуть пересчёт того, что уже посчитано в рамках
    того же операционного окна). Резюме прерванного прогона: ровно этот
    сценарий — сначала полный прогон остановлен по ETA-порогу на
    1178/2130, потом добит --only-missing без повторной траты времени
    на уже готовые 1178.

    Возвращает и СТАРЫЕ ключи (written/no_coords/drifted/total — тесты
    уже на них завязаны), и НОВЫЕ (processed/succeeded/failed/skipped/
    failed_ids — задача, явно: "итоговый отчёт processed/succeeded/
    failed/skipped")."""
    from bot.db.pg import fetch
    from bot.git_info import git_hash

    where = "WHERE COALESCE(is_garbage, FALSE) = FALSE AND COALESCE(is_street, FALSE) = FALSE"
    params: list = []
    if complex_ids is not None:
        params.append(complex_ids)
        where += f" AND id = ANY(${len(params)}::int[])"
    if only_missing:
        where += (" AND NOT EXISTS (SELECT 1 FROM complex_location_scores s "
                   "WHERE s.complex_id = complexes.id AND s.computed_at::date = CURRENT_DATE)")
    order_limit = " ORDER BY id"
    if limit:
        order_limit += f" LIMIT {int(limit)}"
    rows = await fetch(f"SELECT id, name, year_built, district FROM complexes {where}{order_limit}", *params)

    commit = git_hash()
    sem = asyncio.Semaphore(_CONCURRENCY)
    # return_exceptions=True — задача 2026-08-17: одно упавшее (Overpass/
    # БД/парсинг) НЕ должно оборвать asyncio.gather() целиком и потерять
    # результаты уже посчитанных/ещё считающихся complexes. Раньше
    # первое же исключение из _process_one пробрасывалось наружу
    # немедленно — весь batch падал, даже если проблема была ровно в
    # одном ЖК (например house_resolution не смогла его геокодировать
    # по редкой причине).
    outcomes = await asyncio.gather(
        *(_process_one(r, commit, sem, dry_run) for r in rows), return_exceptions=True)

    written = drifted = no_coords = failed = 0
    failed_ids: list[dict] = []
    for r, outcome in zip(rows, outcomes):
        if isinstance(outcome, Exception):
            failed += 1
            failed_ids.append({"complex_id": r["id"], "name": r["name"], "error": str(outcome)})
            log.warning("complex_location_score_snapshot: complex_id=%s упал: %s", r["id"], outcome, exc_info=True)
            continue
        if outcome == "no_coords":
            no_coords += 1
        elif outcome == "written_drifted":
            written += 1
            drifted += 1
        elif outcome == "written":
            written += 1

    log.info(
        "complex_location_score_snapshot: written=%d no_coords=%d drifted=%d failed=%d (из %d complexes в скоупе, dry_run=%s)",
        written, no_coords, drifted, failed, len(rows), dry_run,
    )
    return {
        "written": written, "no_coords": no_coords, "drifted": drifted, "total": len(rows),
        "processed": len(rows), "succeeded": written, "failed": failed, "skipped": no_coords,
        "failed_ids": failed_ids, "dry_run": dry_run,
    }


async def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--complex-ids", default=None,
                     help="через запятую — точечный пересчёт (canary/отладка), без флага — все ЖК")
    ap.add_argument("--limit", type=int, default=None, help="ограничить выборку сверху (canary)")
    ap.add_argument("--dry-run", action="store_true", help="считать, но не писать в complex_location_scores")
    ap.add_argument("--only-missing", action="store_true",
                     help="пропустить ЖК, у которых уже есть строка complex_location_scores "
                          "с computed_at сегодняшним числом (докатка прерванного прогона)")
    args = ap.parse_args()
    complex_ids = [int(x) for x in args.complex_ids.split(",")] if args.complex_ids else None

    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    try:
        result = await run_snapshot(complex_ids=complex_ids, limit=args.limit, dry_run=args.dry_run,
                                     only_missing=args.only_missing)
    finally:
        await close_pool()
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
