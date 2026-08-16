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

--match-mode (эволюция за три задачи подряд, 2026-08-16):
  "candidate_only" — ТЕКУЩИЙ ДЕФОЛТ (задача "безопасная инфраструктура
    кандидатов" — прямое следствие audit_property_match_signals.py:
    даже "безопасный" exact_only даёт 4.3% доказанных rejected-пар).
    listing → ВСЕГДА своя provisional property (bootstrap), НИ ОДИН
    сигнал (exact_hash/fuzzy/dedup_listings) не создаёт hard link САМ
    ПО СЕБЕ — все становятся property_match_candidates строками. См.
    докстринг bot/identity/property_linker.py.
  "exact_only" — LEGACY с этой задачи (был дефолтом ДО неё) — линкует
    по точному address_hash, БЕЗ candidate-таблицы. Только исследование.
  "fuzzy" — LEGACY, НЕБЕЗОПАСНЫЙ, только явный opt-in (argparse choices
    не даёт опечатке тихо съехать на любой из двух legacy-режимов) —
    76.9% high-risk совпадений + подтверждённый order-dependency дефект
    (audit_property_linker_fuzzy.py). НЕ для прод-записи.

Запуск:
    venv/bin/python scripts/backfill_property_ids.py --dry-run
    venv/bin/python scripts/backfill_property_ids.py            # реальная запись, candidate_only (дефолт)
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

# match_method (property_match_candidates) -> ключ в stats — короче для
# отчёта, НЕ равен match_method буквально (нашёл на первом прогоне
# тестов: f"{match_method}_candidates" тихо давал 'exact_hash_candidates',
# а stats была заведена с 'exact_candidates' — KeyError).
_CANDIDATE_STAT_KEY = {"exact_hash": "exact_candidates", "fuzzy": "fuzzy_candidates",
                       "dedup_listings": "dedup_candidates"}


async def run_backfill(dry_run: bool = False, limit: int | None = None,
                        listing_ids: list[str] | None = None,
                        match_mode: str = "candidate_only", order_by: str = "id") -> dict:
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
    from bot.identity.property_linker import link_listing_to_property, DryRunCache, BootstrapIndex

    # price/seller_name/is_duplicate/duplicate_of/dup_match — нужны
    # candidate_only режиму (evidence/dedup_listings-сигнал, задача
    # 2026-08-16, "безопасная инфраструктура кандидатов"). exact_only/
    # fuzzy их просто не читают — не мешают.
    _COLUMNS = ("id, address, floor, area, rooms, complex_name, first_seen, last_seen, archived_at, "
                "price, seller_name, is_duplicate, duplicate_of, dup_match")
    if listing_ids is not None:
        rows = await fetch(
            f"SELECT {_COLUMNS} FROM apartment_listings WHERE id = ANY($1::text[]) ORDER BY id",
            listing_ids,
        )
    else:
        sql = f"SELECT {_COLUMNS} FROM apartment_listings ORDER BY id"
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
    if match_mode == "candidate_only":
        # provisional_created — задача, п.5: "provisional properties to
        # create" — отдельно от auto_new (exact_only/fuzzy семантика
        # "нашли совпадение и НЕ создали") здесь не применима: bootstrap
        # ВСЕГДА создаёт, никогда не находит существующую.
        stats.update({"provisional_created": 0, "exact_candidates": 0,
                      "fuzzy_candidates": 0, "dedup_candidates": 0, "rejected_by_conflict": 0})
    dry_run_cache = BootstrapIndex() if match_mode == "candidate_only" else DryRunCache()

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
        elif result["method"] == "bootstrap":
            stats["provisional_created"] += 1
            for c in result.get("candidates", []):
                # match_method 'dedup_listings'/'exact_hash' -> статистика
                # 'dedup_candidates'/'exact_candidates' (короче для отчёта,
                # НЕ совпадает буквально с match_method — явная карта,
                # не строковая конкатенация, которая тут же и разошлась).
                stats[_CANDIDATE_STAT_KEY[c["match_method"]]] += 1
                if c.get("rejected_by_conflict"):
                    stats["rejected_by_conflict"] += 1
        elif result["method"] == "auto":
            stats["auto_new" if result["created"] else "auto_existing"] += 1
            if result.get("fuzzy_candidate") is not None:
                stats["fuzzy_candidates_logged"] += 1
        if i % 5000 == 0:
            log.info("прогресс: %d/%d", i, len(rows))

    stats["properties_total_after"] = await fetchval("SELECT count(*) FROM properties")
    if match_mode == "candidate_only" and not dry_run:
        stats["candidates_total_after"] = await fetchval("SELECT count(*) FROM property_match_candidates")
    return stats


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="ничего не писать, только сводка")
    ap.add_argument("--limit", type=int, default=None, help="ограничить выборку (отладка)")
    ap.add_argument("--match-mode", choices=["candidate_only", "exact_only", "fuzzy"], default="candidate_only",
                     help="candidate_only (безопасный, дефолт — bootstrap + property_match_candidates, "
                          "НИКАКИХ hard link на совпадение) | exact_only (LEGACY, hard link по точному "
                          "хэшу) | fuzzy (LEGACY, НЕБЕЗОПАСНЫЙ, только исследование — "
                          "см. audit(property-linker): fuzzy match quality audit)")
    ap.add_argument("--order-by", default="id",
                     choices=["id", "id_desc", "listed_at", "listed_at_desc",
                              "shuffled_42", "shuffled_1337"],
                     help="для проверки order-independence (задача 2026-08-16, п.3), дефолт id (как раньше)")
    args = ap.parse_args()

    if args.match_mode != "candidate_only" and not args.dry_run:
        log.warning("match_mode=%s БЕЗ --dry-run — LEGACY режим, создаёт hard link на совпадение "
                    "(candidate_only этого не делает). См. bot/identity/property_linker.py докстринг "
                    "про 'false positive merge хуже false negative duplicate'.", args.match_mode)

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
