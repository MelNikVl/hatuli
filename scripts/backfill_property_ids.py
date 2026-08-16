#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backfill_property_ids.py — Property Identity, разовый backfill (задача
2026-08-16, "P1 — Property Identity"): проход по ВСЕМ apartment_listings,
для каждого — bot.identity.property_linker.link_listing_to_property()
(миграции 083/084 — properties/property_listings).

Идемпотентен: UNIQUE(listing_id) на property_listings + короткое
замыкание "уже связан" в самом линковщике защищают от дублей при
повторном прогоне (второй прогон — все 'already_linked', 0 новых записей).

--dry-run — НИ ОДНОЙ записи в БД, только сводка "сколько properties
получилось бы, сколько linked/unlinked" — см. докстринг
link_listing_to_property() про dry_run_cache (без него дублирующиеся ещё
не виденные квартиры внутри одного прогона задвоились бы в счётчике
'new').

--match-mode (задача 2026-08-16, "безопасный deterministic exact-only
property linker" — прямое следствие audit_property_linker_fuzzy.py: на
реальных данных прежнее fuzzy-правило дало 76.9% high-risk совпадений и
подтверждённый order-dependency дефект). ДЕФОЛТ "exact_only" — см.
докстринг bot/identity/property_linker.py. "fuzzy" — LEGACY, НЕБЕЗОПАСНЫЙ,
только явный opt-in (argparse choices не даёт опечатке тихо съехать на
него), для исследования/сравнения — НЕ для прод-записи.

Запуск:
    venv/bin/python scripts/backfill_property_ids.py --dry-run
    venv/bin/python scripts/backfill_property_ids.py            # реальная запись, exact-only (дефолт)
    venv/bin/python scripts/backfill_property_ids.py --limit 500 --dry-run  # на выборке, для отладки
    venv/bin/python scripts/backfill_property_ids.py --dry-run --match-mode fuzzy  # ТОЛЬКО исследование
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

# scripts/ — не корень репо (в отличие от остальных backfill/snapshot-
# скриптов проекта, все они лежат в корне) — без этого "from bot.db.pg
# import ..." не резолвится: sys.path[0] по умолчанию = каталог самого
# скрипта (scripts/), а не repo root, где лежит пакет bot/.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.FileHandler("backfill_property_ids.log", encoding="utf-8", errors="replace")],
)
log = logging.getLogger("backfill_property_ids")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")


async def run_backfill(dry_run: bool = False, limit: int | None = None,
                        listing_ids: list[str] | None = None,
                        match_mode: str = "exact_only", order_by: str = "id") -> dict:
    """Возвращает сводку: {"total", "already_linked", "auto_existing",
    "auto_new", "fuzzy", "fuzzy_candidates_logged", "skipped",
    "properties_total_after"} — "properties_total_after" — count(*) FROM
    properties ПОСЛЕ прогона (в dry-run — ДО прогона, реального
    изменения нет). "fuzzy_candidates_logged" — задача 2026-08-16
    ("безопасный exact-only property linker"): сколько НОВЫХ properties
    (match_mode="exact_only") были созданы НЕСМОТРЯ на найденный fuzzy-
    кандидат — см. result["fuzzy_candidate"] в bot/identity/property_
    linker.py, только для лога/будущей ручной проверки, НЕ связывает.

    order_by — "id" (дефолт, как раньше) | "id_desc" | "listed_at" |
    "listed_at_desc" | "shuffled_<seed>" — задача 2026-08-16, п.3
    (детерминированность): позволяет прогнать backfill в разных
    порядках БЕЗ отдельного скрипта, для проверки order-independence
    ИМЕННО через реальный async-путь (не только через симуляцию в
    scripts/audit_property_linker_fuzzy.py).

    listing_ids — опциональный скоуп ТОЛЬКО для дешёвых тестов (тот же
    приём, что complex_walkability_snapshot.py --complex-ids) — прод-путь
    (--limit/весь прогон) им не пользуется."""
    from bot.db.pg import fetch, fetchval
    from bot.identity.property_linker import link_listing_to_property, DryRunCache

    if listing_ids is not None:
        rows = await fetch(
            "SELECT id, address, floor, area, rooms, complex_name, first_seen, last_seen, archived_at "
            "FROM apartment_listings WHERE id = ANY($1::text[]) ORDER BY id",
            listing_ids,
        )
    else:
        sql = ("SELECT id, address, floor, area, rooms, complex_name, first_seen, last_seen, archived_at "
               "FROM apartment_listings ORDER BY id")
        if limit:
            sql += f" LIMIT {int(limit)}"
        rows = await fetch(sql)

    rows = [dict(r) for r in rows]
    if order_by == "id_desc":
        rows.sort(key=lambda r: r["id"], reverse=True)
    elif order_by == "listed_at":
        rows.sort(key=lambda r: r["first_seen"] or r["last_seen"])
    elif order_by == "listed_at_desc":
        rows.sort(key=lambda r: r["first_seen"] or r["last_seen"], reverse=True)
    elif order_by.startswith("shuffled_"):
        import random
        seed = int(order_by.split("_", 1)[1])
        random.Random(seed).shuffle(rows)
    elif order_by != "id":
        raise ValueError(f"неизвестный order_by={order_by!r}")

    stats = {"total": len(rows), "already_linked": 0, "auto_existing": 0,
             "auto_new": 0, "fuzzy": 0, "fuzzy_candidates_logged": 0, "skipped": 0}
    dry_run_cache = DryRunCache()

    for i, row in enumerate(rows, 1):
        result = await link_listing_to_property(
            row, dry_run=dry_run, dry_run_cache=dry_run_cache if dry_run else None,
            match_mode=match_mode)
        if result["method"] == "already_linked":
            stats["already_linked"] += 1
        elif result["method"] == "skipped":
            stats["skipped"] += 1
        elif result["method"] == "fuzzy":
            stats["fuzzy"] += 1
        elif result["method"] == "auto":
            stats["auto_new" if result["created"] else "auto_existing"] += 1
            if result.get("fuzzy_candidate") is not None:
                stats["fuzzy_candidates_logged"] += 1
        if i % 5000 == 0:
            log.info("прогресс: %d/%d", i, len(rows))

    stats["properties_total_after"] = await fetchval("SELECT count(*) FROM properties")
    return stats


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="ничего не писать, только сводка")
    ap.add_argument("--limit", type=int, default=None, help="ограничить выборку (отладка)")
    ap.add_argument("--match-mode", choices=["exact_only", "fuzzy"], default="exact_only",
                     help="exact_only (безопасный, дефолт) | fuzzy (LEGACY, НЕБЕЗОПАСНЫЙ, "
                          "только исследование — см. audit(property-linker): fuzzy match quality audit)")
    ap.add_argument("--order-by", default="id",
                     choices=["id", "id_desc", "listed_at", "listed_at_desc",
                              "shuffled_42", "shuffled_1337"],
                     help="для проверки order-independence (задача 2026-08-16, п.3), дефолт id (как раньше)")
    args = ap.parse_args()

    if args.match_mode == "fuzzy" and not args.dry_run:
        log.warning("match_mode=fuzzy БЕЗ --dry-run — LEGACY небезопасный режим, "
                    "греedy fuzzy-assignments БУДУТ записаны в property_listings. "
                    "См. audit(property-linker): fuzzy match quality audit — 76.9%% high-risk.")

    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    try:
        stats = await run_backfill(dry_run=args.dry_run, limit=args.limit,
                                    match_mode=args.match_mode, order_by=args.order_by)
        log.info("ИТОГ (%s, match_mode=%s, order_by=%s): %s",
                 "DRY-RUN, ничего не записано" if args.dry_run else "запись выполнена",
                 args.match_mode, args.order_by, stats)
        print(stats)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
