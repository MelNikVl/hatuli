#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/audit_seller_profile_merge_simulation.py — задача 2026-08-18,
"Property Identity — review calibration", Stage 3: READ-ONLY симуляция
"что изменится в Seller Profile, если применить ТОЛЬКО уже подтверждённые
(status='accepted') merge-решения". НИЧЕГО не пишет — ни в seller_profiles,
ни в property_listings/properties (задача, явно: "не переводить
seller_profile_snapshot.py на новые метрики", "не менять Seller Profile
production snapshot"). Только SELECT + in-memory группировка/агрегация,
печатает сравнение.

Переиспользует _normalize_name/_GENERIC_NAME_STOPLIST/
_AMBIGUOUS_NAME_MIN_LISTINGS из seller_profile_snapshot.py напрямую (тот
же принцип, что уже задокументирован в scripts/audit_seller_profile_
property_id.py — единый источник правды для нормализации имени, не
вторая параллельная реализация).

## OLD vs SIMULATED — что именно сравнивается

OLD  — метрики, КАК ОНИ СЕГОДНЯ считаются в seller_profiles (production):
       total_listings_count/relist_count/relist_rate — по listing_id
       (raw, без property_id); avg_true_dom_days — по СЫРОМУ property_id
       (Property Identity, БЕЗ merge — так, как properties.property_id
       выглядит сейчас, 171 отдельная provisional property среди тех,
       что участвуют в accepted-решениях).

SIMULATED — те же продавцы, но property_id ПРОГНАН через union-find по
       ВСЕМ status='accepted' рёбрам (та же логика, что docs/
       property_merge_design.md §0/§9) — каждая группа схлопнута в ОДИН
       canonical property_id. Метрики пересчитаны на этой схлопнутой
       группировке: unique_properties, true_relist_count (>1 listing
       ОДНОГО продавца на ОДНУ canonical property), observed_property_span
       (дни между первым и последним появлением объединённой property),
       concurrent_agent_count (сколько РАЗНЫХ нормализованных имён
       продавца одновременно держали листинги на этой canonical property).

## Асель/Динара — почему это отдельная секция, не просто ещё одна строка

Задача, явно: "не выдавать такие имена за одну доказанную личность".
_AMBIGUOUS_NAME_MIN_LISTINGS/is_ambiguous УЖЕ существует в семантике имени
(строка "асель" = >15 объявлений под этим словом), но merge на уровне
property НЕ решает проблему идентичности человека — objединение property
подтверждает "это та же КВАРТИРА", не "это тот же ЧЕЛОВЕК" (задача,
явно). Секция ниже печатает топ ambiguous-имён и явно показывает: даже
после merge их seller_property_diversity остаётся высоким (много РАЗНЫХ
properties под одним словом-именем) — сильный структурный сигнал "это
не один человек", отдельно от property-merge вопроса.

Запуск:
    venv/bin/python scripts/audit_seller_profile_merge_simulation.py
"""
from __future__ import annotations

import asyncio
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")


async def _build_merge_map() -> dict[int, int]:
    """property_id -> canonical_property_id, union-find по status='accepted'
    рёбрам (та же логика, что docs/property_merge_design.md §0/§1 —
    canonical = больше активных listing'ов сейчас, при равенстве раньше
    first_seen_at, иначе меньший property_id)."""
    from bot.db.pg import fetch

    edges = await fetch("""
        SELECT pl.property_id AS prop_a, pmc.candidate_property_id AS prop_b
        FROM property_match_candidates pmc
        JOIN property_listings pl ON pl.listing_id = pmc.listing_id
        WHERE pmc.status = 'accepted'
    """)

    parent: dict[int, int] = {}

    def find(x: int) -> int:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for r in edges:
        a, b = r["prop_a"], r["prop_b"]
        if a is None or b is None:
            continue
        find(a); find(b)
        union(a, b)

    all_props = {p for r in edges for p in (r["prop_a"], r["prop_b"]) if p is not None}
    if not all_props:
        return {}

    prop_rows = await fetch(
        "SELECT property_id, first_seen_at FROM properties WHERE property_id = ANY($1::int[])",
        list(all_props),
    )
    first_seen_by_prop = {r["property_id"]: r["first_seen_at"] for r in prop_rows}
    active_count_rows = await fetch("""
        SELECT pl.property_id, count(*) FILTER (WHERE al.is_active IS TRUE) AS active_count
        FROM property_listings pl JOIN apartment_listings al ON al.id = pl.listing_id
        WHERE pl.property_id = ANY($1::int[]) GROUP BY pl.property_id
    """, list(all_props))
    active_by_prop = {r["property_id"]: r["active_count"] for r in active_count_rows}

    groups: dict[int, set[int]] = defaultdict(set)
    for p in all_props:
        groups[find(p)].add(p)

    merge_map: dict[int, int] = {}
    for _, members in groups.items():
        canonical = sorted(
            members,
            key=lambda p: (
                -(active_by_prop.get(p, 0)),
                first_seen_by_prop.get(p) or datetime.max.replace(tzinfo=timezone.utc),
                p,
            ),
        )[0]
        for p in members:
            merge_map[p] = canonical
    return merge_map


async def _load_seller_base() -> list[dict]:
    from bot.db.pg import fetch
    rows = await fetch("""
        SELECT al.id, al.seller_name, al.is_active, al.first_seen, al.last_seen,
               ol.relisted_within_60d,
               p.property_id, p.first_seen_at AS property_first_seen_at,
               p.last_seen_at AS property_last_seen_at
        FROM apartment_listings al
        LEFT JOIN outcome_labels ol ON ol.listing_id = al.id
        LEFT JOIN property_listings pl ON pl.listing_id = al.id
        LEFT JOIN properties p ON p.property_id = pl.property_id
        WHERE al.seller_name IS NOT NULL AND btrim(al.seller_name) != ''
    """)
    return [dict(r) for r in rows]


async def main() -> None:
    from bot.db.pg import init_pool, close_pool
    from seller_profile_snapshot import _normalize_name, _GENERIC_NAME_STOPLIST, _AMBIGUOUS_NAME_MIN_LISTINGS

    await init_pool(DATABASE_URL)
    try:
        merge_map = await _build_merge_map()
        listings = await _load_seller_base()
    finally:
        await close_pool()

    print(f"merge_map строит {len(merge_map)} property_id -> canonical (из "
          f"{len(set(merge_map.values()))} итоговых canonical properties)")

    by_seller: dict[str, list[dict]] = defaultdict(list)
    skipped = 0
    for l in listings:
        name_norm = _normalize_name(l["seller_name"])
        if name_norm in _GENERIC_NAME_STOPLIST:
            skipped += 1
            continue
        by_seller[name_norm].append(l)
    print(f"продавцов (после generic-стоплиста, {skipped} объявлений пропущено): {len(by_seller)}")

    # ── property-level concurrent-agent map: canonical_property_id -> set(seller_name_norm) ──
    seller_by_canonical_property: dict[int, set[str]] = defaultdict(set)
    for name_norm, group in by_seller.items():
        for l in group:
            pid = l.get("property_id")
            if pid is None:
                continue
            canonical = merge_map.get(pid, pid)
            seller_by_canonical_property[canonical].add(name_norm)

    rows = []
    for name_norm, group in by_seller.items():
        total = len(group)
        old_relist_count = sum(1 for l in group if l.get("relisted_within_60d") is True)

        old_props = {l["property_id"] for l in group if l.get("property_id") is not None}
        old_span_days = []
        for pid in old_props:
            first_at = next((l["property_first_seen_at"] for l in group if l.get("property_id") == pid), None)
            last_at = next((l["property_last_seen_at"] for l in group if l.get("property_id") == pid), None)
            if first_at and last_at:
                old_span_days.append((last_at - first_at).days)

        canonical_props: dict[int, list[dict]] = defaultdict(list)
        for l in group:
            pid = l.get("property_id")
            if pid is None:
                continue
            canonical_props[merge_map.get(pid, pid)].append(l)

        unique_properties_new = len(canonical_props)
        true_relist_count_new = sum(1 for members in canonical_props.values() if len(members) > 1)
        span_new = []
        for canonical, members in canonical_props.items():
            firsts = [m["property_first_seen_at"] for m in members if m.get("property_first_seen_at")]
            lasts = [m["property_last_seen_at"] for m in members if m.get("property_last_seen_at")]
            if firsts and lasts:
                span_new.append((max(lasts) - min(firsts)).days)
        concurrent_counts = [
            len(seller_by_canonical_property[c]) for c in canonical_props if c in seller_by_canonical_property
        ]
        seller_property_diversity = round(unique_properties_new / total, 3) if total else None
        is_ambiguous = total > _AMBIGUOUS_NAME_MIN_LISTINGS

        rows.append({
            "seller_name": name_norm, "total_listings": total,
            "old_relist_count": old_relist_count,
            "old_unique_property_count": len(old_props),
            "old_avg_property_span_days": round(statistics.mean(old_span_days), 1) if old_span_days else None,
            "new_unique_properties": unique_properties_new,
            "new_true_relist_count": true_relist_count_new,
            "new_avg_property_span_days": round(statistics.mean(span_new), 1) if span_new else None,
            "new_avg_concurrent_agents_on_touched_properties": (
                round(statistics.mean(concurrent_counts), 2) if concurrent_counts else None),
            "new_max_concurrent_agents_on_touched_properties": max(concurrent_counts) if concurrent_counts else None,
            "seller_property_diversity": seller_property_diversity,
            "is_ambiguous": is_ambiguous,
        })

    # ── Топ по числу объявлений — где merge реальнее всего меняет картину ──
    rows.sort(key=lambda r: r["total_listings"], reverse=True)
    print("\n=== Топ-25 продавцов по total_listings (old vs simulated new) ===")
    header = ("seller_name", "total", "old_relist", "old_uniq_prop", "old_span_d",
              "new_uniq_prop", "new_true_relist", "new_span_d", "avg_concurrent", "max_concurrent",
              "prop_diversity", "ambiguous")
    print(" | ".join(header))
    for r in rows[:25]:
        print(" | ".join(str(r[k]) for k in [
            "seller_name", "total_listings", "old_relist_count", "old_unique_property_count",
            "old_avg_property_span_days", "new_unique_properties", "new_true_relist_count",
            "new_avg_property_span_days", "new_avg_concurrent_agents_on_touched_properties",
            "new_max_concurrent_agents_on_touched_properties", "seller_property_diversity", "is_ambiguous",
        ]))

    # ── Aggregate summary (across ALL sellers touched by at least one accepted-merge property) ──
    touched = [r for r in rows if r["seller_name"] in
               {n for n, g in by_seller.items() for l in g if l.get("property_id") in merge_map}]
    print(f"\n=== Продавцов, затронутых хотя бы одним merge (из {len(rows)} всего): {len(touched)} ===")
    if touched:
        print("sum(old_unique_property_count) =", sum(r["old_unique_property_count"] for r in touched))
        print("sum(new_unique_properties)      =", sum(r["new_unique_properties"] for r in touched))
        print("sum(old_relist_count)           =", sum(r["old_relist_count"] for r in touched))
        print("sum(new_true_relist_count)      =", sum(r["new_true_relist_count"] for r in touched))

    # ── Асель/Динара — ambiguous-name section (задача, явно) ──
    ambiguous_rows = sorted([r for r in rows if r["is_ambiguous"]], key=lambda r: r["total_listings"], reverse=True)
    print(f"\n=== Ambiguous имена (>{_AMBIGUOUS_NAME_MIN_LISTINGS} объявлений под одним словом-именем): "
          f"{len(ambiguous_rows)} ===")
    print("Задача, явно: НЕ считать это одной доказанной личностью — property-merge объединяет КВАРТИРЫ, "
          "не подтверждает, что 'Асель'/'Динара' — один и тот же человек.")
    for r in ambiguous_rows[:15]:
        print(f"  {r['seller_name']!r}: total_listings={r['total_listings']}, "
              f"new_unique_properties={r['new_unique_properties']}, "
              f"seller_property_diversity={r['seller_property_diversity']} "
              f"({'высокое разнообразие properties -> вероятно РАЗНЫЕ люди под одним именем' if (r['seller_property_diversity'] or 0) > 0.5 else 'ниже, но всё равно не доказательство одной личности'})")


if __name__ == "__main__":
    asyncio.run(main())
