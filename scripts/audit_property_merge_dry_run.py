#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/audit_property_merge_dry_run.py — задача 2026-08-20, "Safe
Physical Property Merge", раздел 10: read-only real-data dry-run отчёт
поверх bot/identity/property_merge.py (та же engine-логика, что реальный
--apply использовал бы, включая ревалидацию — НЕ отдельная/упрощённая
копия). Ничего не пишет, ничего не мерджит, ни один candidate status не
меняется.

Дополнительно к plan_property_merge() классифицирует BLOCKED компоненты
на 'stale evidence' (текущий hard conflict, которого НЕ было в evidence_
snapshot property_match_review_log на момент accept — данные утекли уже
ПОСЛЕ решения человека) vs 'standing conflict' (конфликт уже был виден
человеку в момент accept, он решил принять пару всё равно) — эта
классификация ТОЛЬКО для отчёта, движок сам эту разницу не использует
(блокирует в обоих случаях одинаково, задача явно требует так)."""
from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")


async def _classify_blocked(plan: dict) -> str:
    """'stale_evidence' -> хоть одна блокирующая проблема на ребре, чей
    evidence_snapshot (на момент accept) НЕ показывал этот конфликт;
    'standing_conflict' -> конфликт уже был виден человеку на момент
    решения (rooms_a/rooms_b в снимке уже расходились), он всё равно
    принял пару — это другая категория внимания оператора."""
    from bot.db.pg import fetchrow
    import json as _json

    for problem in plan["blocked_reasons"]:
        cid = problem.get("candidate_id")
        if cid is None:
            continue
        row = await fetchrow(
            "SELECT evidence_snapshot FROM property_match_review_log WHERE candidate_id=$1 "
            "ORDER BY id DESC LIMIT 1", cid)
        if row is None or row["evidence_snapshot"] is None:
            continue
        snap = row["evidence_snapshot"]
        snap = _json.loads(snap) if isinstance(snap, str) else snap
        if problem["reason"] == "rooms_mismatch":
            if snap.get("rooms_a") == snap.get("rooms_b"):
                return "stale_evidence"
    return "standing_conflict"


async def main() -> None:
    from bot.db.pg import close_pool, init_pool, fetchval
    from bot.identity.property_merge import plan_property_merge

    await init_pool(DATABASE_URL)
    try:
        total_accepted = await fetchval("SELECT count(*) FROM property_match_candidates WHERE status='accepted'")
        plans = await plan_property_merge(None)

        planned = [p for p in plans if p["status"] == "planned"]
        blocked = [p for p in plans if p["status"] == "blocked"]

        classified: dict[str, list[dict]] = {"stale_evidence": [], "standing_conflict": []}
        for p in blocked:
            bucket = await _classify_blocked(p)
            classified[bucket].append(p)

        # "ambiguous / manual re-review needed" — planned-компоненты, где
        # СОХРАНЁННЫЙ relationship_type расходится с ПЕРЕСЧИТАННЫМ сейчас
        # (не блокирует — задача явно: concurrent-vs-relist mismatch не
        # конфликт — но стоит показать оператору отдельно перед --apply).
        ambiguous = [p for p in planned
                     if any(not r["matches"] for r in p["manifest"]["evidence_snapshot"]["current_relationship"])]
    finally:
        await close_pool()

    print("=" * 70)
    print("SAFE PHYSICAL PROPERTY MERGE — REAL-DATA DRY RUN (read-only, задача 2026-08-20 п.10)")
    print("=" * 70)
    print(f"accepted candidate pairs total: {total_accepted}")
    print(f"connected components total: {len(plans)}")

    sizes = sorted((len(p["members"]) for p in plans), reverse=True)
    dist = {s: sizes.count(s) for s in sorted(set(sizes), reverse=True)}
    print(f"component size distribution: {dist}")
    print(f"largest component size: {sizes[0] if sizes else 0}")

    print()
    print(f"mergeable now (planned):            {len(planned)}")
    print(f"blocked — stale evidence:            {len(classified['stale_evidence'])}")
    print(f"blocked — standing conflict:          {len(classified['standing_conflict'])}")
    print(f"ambiguous (relationship_type stale,    but NOT blocking): {len(ambiguous)}")

    total_properties_mergeable = sum(len(p["members"]) for p in planned)

    print()
    print(f"properties that would be touched by a mergeable-now component: {total_properties_mergeable}")
    moved_listings_total = sum(
        len(lst) for p in planned for lst in p["manifest"]["evidence_snapshot"]["moved_listing_ids"].values()
    )
    print(f"listing rows that would be repointed (property_listings.property_id): {moved_listings_total}")

    print()
    print("-- 10 показательных компонентов --")
    shown = 0
    for p in sorted(plans, key=lambda x: -len(x["members"]))[:10]:
        shown += 1
        if p["status"] == "planned":
            m = p["manifest"]
            print(f"  [{shown}] PLANNED size={len(p['members'])} canonical={p['canonical_property_id']} "
                  f"losing={p['losing_property_ids']}")
            print(f"       candidate_ids={m['candidate_ids'][:5]}{'...' if len(m['candidate_ids']) > 5 else ''}  "
                  f"component_hash={m['component_hash'][:16]}...")
        else:
            reasons = sorted({r['reason'] for r in p['blocked_reasons']})
            print(f"  [{shown}] BLOCKED size={len(p['members'])} members={p['members']} reasons={reasons}")
            for r in p["blocked_reasons"][:3]:
                print(f"       - {r['reason']}: {r['detail']}")

    print()
    print("-- Самый большой компонент (отдельная проверка, задача явно требует) --")
    biggest = max(plans, key=lambda p: len(p["members"]))
    print(f"  size={len(biggest['members'])} members={biggest['members']} status={biggest['status']}")
    if biggest["status"] == "planned":
        m = biggest["manifest"]
        print(f"  canonical={biggest['canonical_property_id']}  losing={biggest['losing_property_ids']}")
        print(f"  matcher_version={m['matcher_version']}")
        print(f"  moved_listing_ids: {m['evidence_snapshot']['moved_listing_ids']}")
        scoring_top = m["evidence_snapshot"]["scoring"][0]
        print(f"  winning score breakdown: {scoring_top}")
    else:
        for r in biggest["blocked_reasons"]:
            print(f"  BLOCKED: {r['reason']}: {r['detail']}")

    print()
    print("=" * 70)
    print("НИЧЕГО не записано в БД — этот скрипт read-only от начала до конца.")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
