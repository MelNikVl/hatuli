#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/recompute_candidate_corroboration.py — задача 2026-08-17,
"Property Identity — photo evidence + review", часть C: пересчитывает
evidence->corroborating_methods для существующих property_match_
candidates строк (bot.identity.property_linker::recompute_corroborating_
methods — сама логика, эта обёртка только батчит вызовы + отчёт).

Идемпотентно (задача, явно) — повторный прогон на ту же пару без
изменения данных даёт тот же список, безопасно гонять регулярно
(например, ПОСЛЕ scripts/photo_evidence_scan.py, чтобы photo_exact/
photo_perceptual/photo_ai попали в corroborating_methods у уже
существующих кандидатов).

Запуск:
    venv/bin/python scripts/recompute_candidate_corroboration.py --dry-run --limit 20
    venv/bin/python scripts/recompute_candidate_corroboration.py --status pending --limit 500
    venv/bin/python scripts/recompute_candidate_corroboration.py --with-photo-evidence-only
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("recompute_candidate_corroboration")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")


async def _select_candidates(status_list: list[str], limit: int | None,
                              with_photo_evidence_only: bool) -> list[int]:
    from bot.db.pg import fetch
    where = ["pmc.status = ANY($1::text[])"]
    params: list = [status_list]
    if with_photo_evidence_only:
        where.append("EXISTS (SELECT 1 FROM property_candidate_photo_evidence pcpe "
                      "WHERE pcpe.candidate_id = pmc.candidate_id)")
    sql = (f"SELECT pmc.candidate_id FROM property_match_candidates pmc WHERE {' AND '.join(where)} "
           f"ORDER BY pmc.candidate_id")
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = await fetch(sql, *params)
    return [r["candidate_id"] for r in rows]


async def run(status_list: list[str], limit: int | None, with_photo_evidence_only: bool,
              dry_run: bool, batch_size: int) -> dict:
    from bot.identity.property_linker import recompute_corroborating_methods

    ids = await _select_candidates(status_list, limit, with_photo_evidence_only)
    log.info("найдено %d кандидатов для пересчёта corroborating_methods%s",
              len(ids), " [DRY-RUN]" if dry_run else "")

    method_counts: dict[str, int] = {}
    multi_method_count = 0
    examples = []
    if dry_run:
        # recompute_corroborating_methods() сама не умеет dry-run (это
        # ИДЕМПОТЕНТНЫЙ пересчёт evidence, не "решение" — писать его же
        # снова безопасно) — --dry-run здесь просто НЕ вызывает её вовсе,
        # честно ничего не трогая, отчёт только "нашёл бы N кандидатов".
        return {"total": len(ids), "method_counts": {}, "multi_method_pairs": 0, "examples": []}

    for i, cid in enumerate(ids):
        found = await recompute_corroborating_methods(cid)
        for m in found:
            method_counts[m] = method_counts.get(m, 0) + 1
        if len(found) > 1:
            multi_method_count += 1
            if len(examples) < 15:
                examples.append({"candidate_id": cid, "corroborating_methods": found})
        if (i + 1) % max(batch_size, 1) == 0:
            log.info("прогресс: %d/%d", i + 1, len(ids))

    return {"total": len(ids), "method_counts": method_counts,
            "multi_method_pairs": multi_method_count, "examples": examples}


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="только посчитать сколько кандидатов, не пересчитывать")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--batch-size", type=int, default=100)
    ap.add_argument("--status", type=str, default="pending,rejected", help="csv статусов")
    ap.add_argument("--with-photo-evidence-only", action="store_true",
                     help="только кандидаты, для которых уже есть property_candidate_photo_evidence")
    args = ap.parse_args()

    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    t0 = time.monotonic()
    try:
        status_list = [s.strip() for s in args.status.split(",") if s.strip()]
        report = await run(status_list, args.limit, args.with_photo_evidence_only, args.dry_run, args.batch_size)
    finally:
        await close_pool()

    elapsed = round(time.monotonic() - t0, 1)
    log.info("ИТОГ (%.1fс): total=%d multi_method_pairs=%d method_counts=%s",
              elapsed, report["total"], report["multi_method_pairs"], report["method_counts"])
    print({"elapsed_sec": elapsed, **{k: v for k, v in report.items() if k != "examples"}})
    for ex in report["examples"]:
        print(ex)


if __name__ == "__main__":
    asyncio.run(main())
