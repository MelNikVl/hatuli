#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/photo_evidence_scan.py — задача 2026-08-17, "Property Identity —
photo evidence", часть B, ФАЗА 1 (main venv, БЕЗ torch): sha256 + perceptual
hash для фотографий сторон уже найденных property_match_candidates пар,
плюс агрегация exact_shared/perceptual_shared в property_candidate_photo_
evidence. НЕ трогает embedding/ai_similar_count/photo_type — это ФАЗА 2
(scripts/photo_evidence_ai_scan.py, отдельный venv, отдельный запуск).

НИКАКОГО автоматического merge (задача, явно) — этот скрипт только копит
evidence, ничего не пишет в property_listings/properties, не меняет
property_match_candidates.status.

## Область (задача, явно): "не выполнять глобальное попарное сравнение
всех 50 тысяч объявлений. Сравнивать все фотографии только внутри уже
найденных пар-кандидатов Property Identity"

Обрабатывает СТРОКИ property_match_candidates (--status фильтрует
status, дефолт 'pending' — самое операционно полезное подмножество,
'rejected' тоже можно явно попросить --status pending,rejected для
impact-анализа soft-conflict пересмотра, задача D). Каждая обработка —
максимум фото одного listing'а × максимум фото другого (≤15×15 по
лимиту bot/core/apartment_details.py), НЕ полный граф listing'ов.

## Порядок / приоритет

--order strongest (дефолт) — match_score DESC, candidate_id ASC (tie-break,
детерминированно) — задача F, canary: "100 наиболее сильных candidate
pairs". --order id — candidate_id ASC, для полного планового прогона по
очереди (не только самых сильных).

## Идемпотентность / возобновление

--only-missing (тот же принцип, что --only-missing в scripts/location_
score_snapshot.py и др. в этом репо) — пропускает candidate'ов, для
которых property_candidate_photo_evidence.processing_status уже 'ok'
(не 'pending'/'partial'/'error' — те стоит пересчитать: partial значит
AI-стадия ещё не отработала фото, error — стоит попробовать снова).

## Запуск

    venv/bin/python scripts/photo_evidence_scan.py --canary                       # 100 сильнейших, отчёт распределения
    venv/bin/python scripts/photo_evidence_scan.py --dry-run --limit 20           # не пишет evidence
    venv/bin/python scripts/photo_evidence_scan.py --limit 200 --only-missing     # батч, пропуская уже готовые
    venv/bin/python scripts/photo_evidence_scan.py --status pending,rejected --limit 500
    venv/bin/python scripts/photo_evidence_scan.py --only-missing --reuse-existing-fingerprints
        # после AI-стадии: только reaggregation, без CDN-загрузок
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.FileHandler("photo_evidence_scan.log", encoding="utf-8", errors="replace"),
              logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("photo_evidence_scan")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")
_DEFAULT_PHOTO_DELAY = 1.0  # между РЕАЛЬНЫМИ (не кэш) закачками фото — тот же порядок, что floorplan_scan.py


async def _select_candidates(status_list: list[str], limit: int | None, order: str,
                              only_missing: bool, min_score: float | None) -> list[dict]:
    from bot.db.pg import fetch

    where = ["pmc.status = ANY($1::text[])"]
    params: list = [status_list]
    if min_score is not None:
        params.append(min_score)
        where.append(f"pmc.match_score >= ${len(params)}")
    if only_missing:
        where.append(
            "NOT EXISTS (SELECT 1 FROM property_candidate_photo_evidence pcpe "
            "WHERE pcpe.candidate_id = pmc.candidate_id AND pcpe.processing_status = 'ok')"
        )
    order_sql = "pmc.match_score DESC, pmc.candidate_id ASC" if order == "strongest" else "pmc.candidate_id ASC"
    sql = (
        f"SELECT pmc.candidate_id, pmc.listing_id, pmc.candidate_property_id, pmc.match_score, pmc.status "
        f"FROM property_match_candidates pmc WHERE {' AND '.join(where)} ORDER BY {order_sql}"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = await fetch(sql, *params)
    return [dict(r) for r in rows]


async def run_scan(status_list: list[str], limit: int | None, order: str, only_missing: bool,
                    min_score: float | None, dry_run: bool, batch_size: int,
                    delay: float, reuse_existing_fingerprints: bool = False) -> dict:
    import httpx

    from bot.identity.photo_evidence import aggregate_candidate_evidence

    candidates = await _select_candidates(status_list, limit, order, only_missing, min_score)
    log.info("найдено %d candidate-пар (status=%s, order=%s, only_missing=%s)%s",
              len(candidates), status_list, order, only_missing, " [DRY-RUN]" if dry_run else "")

    stats = {
        "found": len(candidates), "processed": 0, "ok": 0, "partial": 0, "error": 0,
        "exact_pairs": 0, "perceptual_pairs": 0,
        "with_exact": 0, "with_perceptual": 0, "with_no_match": 0,
    }
    examples: list[dict] = []

    async with httpx.AsyncClient(timeout=20.0) as client:
        for i, c in enumerate(candidates):
            try:
                evidence = await aggregate_candidate_evidence(
                    c["candidate_id"], http_client=client, delay=delay, dry_run=dry_run,
                    reuse_existing_fingerprints=reuse_existing_fingerprints)
            except Exception as exc:  # noqa: BLE001 — один упавший кандидат не должен ронять весь батч
                log.warning("candidate_id=%s упал: %s: %s", c["candidate_id"], type(exc).__name__, exc)
                stats["error"] += 1
                continue

            stats["processed"] += 1
            status = evidence.get("processing_status", "error")
            stats[status] = stats.get(status, 0) + 1
            exact = evidence.get("exact_shared_count", 0)
            perceptual = evidence.get("perceptual_shared_count", 0)
            stats["exact_pairs"] += exact
            stats["perceptual_pairs"] += perceptual
            if exact > 0:
                stats["with_exact"] += 1
            elif perceptual > 0:
                stats["with_perceptual"] += 1
            else:
                stats["with_no_match"] += 1

            if len(examples) < 20 and (exact > 0 or perceptual > 0):
                examples.append({"candidate_id": c["candidate_id"], "listing_id": c["listing_id"],
                                  "candidate_property_id": c["candidate_property_id"],
                                  "match_score": float(c["match_score"]) if c["match_score"] is not None else None,
                                  **{k: v for k, v in evidence.items() if k != "matched_photos"}})

            if (i + 1) % max(batch_size, 1) == 0 or (i + 1) == len(candidates):
                log.info("прогресс: %d/%d — %s", i + 1, len(candidates),
                          {k: v for k, v in stats.items() if k != "found"})

    stats["examples"] = examples
    return stats


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="не писать property_candidate_photo_evidence")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--batch-size", type=int, default=25, help="частота прогресс-лога")
    ap.add_argument("--status", type=str, default="pending", help="csv статусов property_match_candidates")
    ap.add_argument("--order", choices=["strongest", "id"], default="strongest")
    ap.add_argument("--only-missing", action="store_true",
                     help="пропускать кандидатов с уже готовым (processing_status='ok') evidence")
    ap.add_argument("--min-score", type=float, default=None)
    ap.add_argument("--delay", type=float, default=_DEFAULT_PHOTO_DELAY,
                     help="пауза перед каждой РЕАЛЬНОЙ (не из кэша) закачкой фото")
    ap.add_argument("--reuse-existing-fingerprints", action="store_true",
                    help="пересчитать evidence только по сохранённым fingerprint: без CDN-загрузок")
    ap.add_argument("--canary", action="store_true",
                     help="эквивалент --limit 100 --order strongest, печатает распределение exact/perceptual")
    args = ap.parse_args()

    status_list = [s.strip() for s in args.status.split(",") if s.strip()]
    limit = args.limit
    order = args.order
    if args.canary:
        limit = limit or 100
        order = "strongest"

    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    t0 = time.monotonic()
    try:
        stats = await run_scan(status_list, limit, order, args.only_missing, args.min_score,
                               args.dry_run, args.batch_size, args.delay,
                               args.reuse_existing_fingerprints)
    finally:
        await close_pool()

    elapsed = round(time.monotonic() - t0, 1)
    log.info("ИТОГ (%.1fс): %s", elapsed, {k: v for k, v in stats.items() if k != "examples"})
    print({"elapsed_sec": elapsed, **{k: v for k, v in stats.items() if k != "examples"}})
    if args.canary:
        print("\n=== canary: примеры пар с совпавшими фото ===")
        for ex in stats["examples"]:
            print(ex)


if __name__ == "__main__":
    asyncio.run(main())
