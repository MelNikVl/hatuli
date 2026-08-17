#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/audit_conflict_reclassification_impact.py — задача 2026-08-17,
"Property Identity — photo evidence + review", часть D: "Перед изменением
уже существующих статусов сделать read-only impact audit: сколько rejected
станет review_required, сколько из них имеет dedup/photo corroboration.
Ничего массово не менять без отчёта и моего ОК."

READ-ONLY. НЕ пишет ничего — ни в property_match_candidates.status
(существующие 'rejected' строки в БД остаются 'rejected' до отдельного,
явно одобренного data-fix прогона), ни куда-либо ещё.

## Что считает

bot/identity/property_linker.py::_is_hard_conflict() уже изменена (эта же
задача, часть D, код-логика) — ТОЛЬКО rooms mismatch форсирует rejected
для НОВЫХ кандидатов. house_number/price mismatch теперь soft (кандидат
остаётся pending). Этот аудит применяет ТУ ЖЕ функцию к conflict_reasons
уже существующих 'rejected' строк — "сколько из них были бы pending
('review_required'), если бы правило действовало на момент их создания".

Для каждой такой строки — corroboration (переиспользован bot/identity/
property_linker.py::_corroborating_base_methods/_corroborating_photo_
methods, НЕ вторая копия той же логики):
  - dedup_corroboration — pair ТАКЖЕ согласуется через dedup_listings
    (независимо от того, каким match_method сама строка была найдена);
  - photo_corroboration — property_candidate_photo_evidence уже посчитана
    для этого candidate_id И exact/perceptual/ai > 0 (на момент ПЕРВОГО
    запуска этого аудита — ДО canary фотографий — ожидаемо 0, задача
    просит перезапустить этот аудит ПОСЛЕ photo evidence canary для
    полной картины, см. отчёт задачи).

Запуск: venv/bin/python scripts/audit_conflict_reclassification_impact.py [--json]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")


async def run_audit(limit: int | None = None) -> dict:
    from bot.db.pg import fetch
    from bot.identity.property_linker import (
        _is_hard_conflict, _corroborating_base_methods, _corroborating_photo_methods,
    )

    sql = ("SELECT candidate_id, listing_id, candidate_property_id, match_method, "
           "match_score, relationship_type, conflict_reasons "
           "FROM property_match_candidates WHERE status = 'rejected' ORDER BY candidate_id")
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = [dict(r) for r in await fetch(sql)]

    total = len(rows)
    would_become_review = []
    stays_rejected_rooms = 0

    for r in rows:
        reasons = json.loads(r["conflict_reasons"]) if isinstance(r["conflict_reasons"], str) else (
            r["conflict_reasons"] or [])
        if _is_hard_conflict(reasons):
            stays_rejected_rooms += 1
            continue
        would_become_review.append({**r, "conflict_reasons": reasons})

    dedup_corrob = photo_corrob = both_corrob = neither_corrob = 0
    examples_no_corrob = []
    examples_with_corrob = []

    for r in would_become_review:
        listing_row = await fetch(
            "SELECT id, address, floor, area, complex_name, duplicate_of, dup_match "
            "FROM apartment_listings WHERE id = $1", r["listing_id"])
        prop_row = await fetch(
            "SELECT property_id, complex_id, address_hash, floor, area_sqm FROM properties "
            "WHERE property_id = $1", r["candidate_property_id"])
        has_dedup = has_photo = False
        if listing_row and prop_row:
            base_methods = await _corroborating_base_methods(dict(listing_row[0]), dict(prop_row[0]))
            has_dedup = "dedup_listings" in base_methods
        photo_methods = await _corroborating_photo_methods(r["candidate_id"])
        has_photo = len(photo_methods) > 0

        if has_dedup:
            dedup_corrob += 1
        if has_photo:
            photo_corrob += 1
        if has_dedup and has_photo:
            both_corrob += 1
        if not has_dedup and not has_photo:
            neither_corrob += 1
            if len(examples_no_corrob) < 10:
                examples_no_corrob.append(r)
        else:
            if len(examples_with_corrob) < 10:
                examples_with_corrob.append({**r, "dedup_corroboration": has_dedup,
                                              "photo_corroboration": has_photo})

    return {
        "total_rejected": total,
        "stays_rejected_rooms_mismatch": stays_rejected_rooms,
        "would_become_review_required": len(would_become_review),
        "of_those_with_dedup_corroboration": dedup_corrob,
        "of_those_with_photo_corroboration": photo_corrob,
        "of_those_with_both": both_corrob,
        "of_those_with_neither": neither_corrob,
        "examples_no_corroboration": examples_no_corrob,
        "examples_with_corroboration": examples_with_corrob,
    }


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--limit", type=int, default=None, help="ограничить выборку rejected-строк (отладка)")
    args = ap.parse_args()

    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    try:
        report = await run_audit(args.limit)
    finally:
        await close_pool()

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print("\n=== impact audit: house_number/price -> soft conflict (READ-ONLY, ничего не изменено) ===\n")
        print(f"всего status='rejected' сейчас:              {report['total_rejected']}")
        print(f"останется rejected (rooms mismatch):          {report['stays_rejected_rooms_mismatch']}")
        print(f"стало бы review_required (pending):           {report['would_become_review_required']}")
        print(f"  из них с dedup_listings corroboration:      {report['of_those_with_dedup_corroboration']}")
        print(f"  из них с photo corroboration:                {report['of_those_with_photo_corroboration']}")
        print(f"  из них с обоими:                             {report['of_those_with_both']}")
        print(f"  из них БЕЗ независимого подтверждения:       {report['of_those_with_neither']}")
        print("\n--- примеры БЕЗ corroboration (первые 10) ---")
        for e in report["examples_no_corroboration"]:
            print(e)
        print("\n--- примеры С corroboration (первые 10) ---")
        for e in report["examples_with_corroboration"]:
            print(e)


if __name__ == "__main__":
    asyncio.run(main())
