#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/audit_seller_profile_merge_simulation.py — задача 2026-08-18,
"Property Identity — review calibration" (Stage 3) + follow-up
"validation" (п.5, исправление): READ-ONLY симуляция "что изменится в
Seller Profile, если применить ТОЛЬКО уже подтверждённые (status=
'accepted') merge-решения". НИЧЕГО не пишет — ни в seller_profiles, ни в
property_listings/properties.

## Исправление follow-up п.5 — что было не так в первой версии

Первая версия печатала "old_relist_count: 28 -> new_true_relist_count: 13"
как будто это одна метрика "до/после merge". Это НЕЧЕСТНОЕ сравнение —
`old_relist_count` = `outcome_labels.relisted_within_60d` — ЛИСТИНГОВАЯ
эвристика, которая НИКОГДА не смотрит на property_id вообще (она либо
есть, либо её нет, независимо от того, смерджены properties или нет).
Сравнивать её с property-based "true relist" — сравнение ДВУХ РАЗНЫХ
ФОРМУЛ, не "было/стало" одной формулы.

Эта версия считает 4 ЧЕСТНЫХ числа на одной и той же совокупности:
  1. old_formula_before  — relisted_within_60d, БЕЗ merge (= с merge,
     см. ниже: тождественно равно #2, потому что формула НЕ использует
     property_id — это ПОКАЗАНО явно, не скрыто).
  2. old_formula_after   — та же формула, "после" симуляции merge.
  3. new_true_relist_before — property-based (>1 listing ОДНОГО продавца
     на ОДНУ property), на СЫРОМ (НЕ смерженном) property_id — так, как
     Property Identity выглядит на проде СЕЙЧАС, без единого merge.
  4. new_true_relist_after  — та же формула, PropertyId прогнан через
     union-find по 'accepted'-рёбрам (симуляция после merge).

Разница #3 vs #4 — это и есть ЧИСТЫЙ эффект 101 accepted-решения на
property-based relist-метрику, без путаницы со старой листинговой
эвристикой.

## multi_agent vs true_relist — по компонентам, не по продавцам

Задача, явно: "сколько компонент являются multi-agent exposure, сколько —
настоящий relist". Для КАЖДОЙ merge-компоненты (70 компонент, союз всех
'accepted'-properties через union-find) считается МНОЖЕСТВО нормализо-
ванных seller_name всех листингов внутри неё:
  - multi_agent  — >1 РАЗНЫХ продавца когда-либо держали листинг на этой
    физической квартире (после merge) — НЕ relist, это то самое разное
    физическое обстоятельство, которое задача просит не путать с relist.
  - true_relist  — хотя бы ОДИН продавец имеет >1 листинга внутри
    компоненты (тот же продавец перевыставлял ЭТУ ЖЕ квартиру).
Компонента может быть И multi_agent, И true_relist одновременно (не
взаимоисключающие категории на уровне компоненты) — печатается полная
матрица 2×2 (задача требует не выдавать сумму без пересечений и здесь,
по аналогии с фото-сигналами в Stage 1.3/follow-up п.1).

## Асель/Динара

Не изменилось: is_ambiguous (>15 объявлений под одним словом-именем) —
ambiguous-имена НЕ считаются доказанной единой идентичностью нигде в
этом файле, даже после merge (высокая seller_property_diversity этого
не меняет — задача, явно).
"""
from __future__ import annotations

import asyncio
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")


async def _build_merge_map() -> tuple[dict[int, int], dict[int, set[int]]]:
    """property_id -> canonical_property_id + сами группы (для компонент-
    level анализа multi_agent/true_relist ниже). Правило выбора
    canonical — ВРЕМЕННОЕ (см. follow-up п.6, docs/property_merge_
    design.md — там уточняется на многофакторный scoring; здесь для
    Seller Profile симуляции важен САМ ФАКТ объединения группы в один
    canonical id, не то, какой именно id выбран каноническим — какая
    property "выиграла" не влияет на relist/multi-agent счёт компоненты
    целиком)."""
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
        return {}, {}

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
    components: dict[int, set[int]] = {}
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
        components[canonical] = members
    return merge_map, components


async def _load_seller_base() -> list[dict]:
    from bot.db.pg import fetch
    rows = await fetch("""
        SELECT al.id, al.seller_name, al.is_active,
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


async def _seller_identity_coverage() -> dict:
    """"Сколько объявлений не имеют надёжной seller identity" (задача,
    явно) — три взаимоисключающих бакета: NULL/пусто, generic-стоплист
    (роль-заглушка, не имя), обычное имя (то, что реально идёт в
    группировку по продавцу)."""
    from bot.db.pg import fetch, fetchval
    from seller_profile_snapshot import _normalize_name, _GENERIC_NAME_STOPLIST

    total = await fetchval("SELECT count(*) FROM apartment_listings")
    null_or_empty = await fetchval(
        "SELECT count(*) FROM apartment_listings WHERE seller_name IS NULL OR btrim(seller_name) = ''")
    named = await fetch(
        "SELECT seller_name FROM apartment_listings WHERE seller_name IS NOT NULL AND btrim(seller_name) != ''")
    generic = sum(1 for r in named if _normalize_name(r["seller_name"]) in _GENERIC_NAME_STOPLIST)
    identified = len(named) - generic
    return {"total_listings": total, "no_seller_name": null_or_empty,
            "generic_role_placeholder": generic, "identified_seller_name": identified}


async def main() -> None:
    from bot.db.pg import init_pool, close_pool
    from seller_profile_snapshot import _normalize_name, _GENERIC_NAME_STOPLIST, _AMBIGUOUS_NAME_MIN_LISTINGS

    await init_pool(DATABASE_URL)
    try:
        merge_map, components = await _build_merge_map()
        listings = await _load_seller_base()
        identity_coverage = await _seller_identity_coverage()
    finally:
        await close_pool()

    print("=== Seller identity coverage (все apartment_listings) ===")
    print(identity_coverage)

    print(f"\nmerge_map: {len(merge_map)} property_id -> canonical, "
          f"{len(components)} итоговых компонент (70 ожидается)")

    by_seller: dict[str, list[dict]] = defaultdict(list)
    skipped = 0
    for l in listings:
        name_norm = _normalize_name(l["seller_name"])
        if name_norm in _GENERIC_NAME_STOPLIST:
            skipped += 1
            continue
        by_seller[name_norm].append(l)
    print(f"продавцов (после generic-стоплиста, {skipped} объявлений пропущено): {len(by_seller)}")

    # ── Компонент-level: multi_agent vs true_relist, 2×2 матрица ──────────
    seller_by_component: dict[int, Counter] = defaultdict(Counter)  # canonical -> Counter(seller_name_norm -> listing_count)
    for name_norm, group in by_seller.items():
        for l in group:
            pid = l.get("property_id")
            if pid is None:
                continue
            canonical = merge_map.get(pid, pid)
            if canonical not in components:
                continue  # эта property не входит ни в одну merge-компоненту
            seller_by_component[canonical][name_norm] += 1

    n_multi_agent = n_true_relist = n_both = n_neither = 0
    seller_profile_scope_components = 0
    for canonical in components:
        counter = seller_by_component.get(canonical, Counter())
        if not counter:
            continue  # ни один листинг с идентифицируемым продавцом
        seller_profile_scope_components += 1
        is_multi_agent = len(counter) > 1
        is_true_relist = any(c > 1 for c in counter.values())
        if is_multi_agent and is_true_relist:
            n_both += 1
        elif is_multi_agent:
            n_multi_agent += 1
        elif is_true_relist:
            n_true_relist += 1
        else:
            n_neither += 1

    print(f"\n=== Компоненты в scope Seller Profile (хотя бы 1 идентифицируемый продавец): "
          f"{seller_profile_scope_components} из {len(components)} ===")
    print("Матрица (multi_agent и true_relist НЕ взаимоисключающие на уровне компоненты — "
          "полное разбиение на 4 непересекающихся класса):")
    print(f"  только multi_agent (разные продавцы, НЕ relist):            {n_multi_agent}")
    print(f"  только true_relist (один продавец, >1 листинг):             {n_true_relist}")
    print(f"  ОБА одновременно (разные продавцы, И у кого-то >1 листинг): {n_both}")
    print(f"  ни то ни другое (1 продавец, 1 листинг в компоненте):       {n_neither}")

    # ── Seller-level: 4 честных числа (old before/after, new before/after) ──
    rows = []
    for name_norm, group in by_seller.items():
        total = len(group)
        # 1/2: old formula — НЕ зависит от property_id вообще, before==after.
        old_relist = sum(1 for l in group if l.get("relisted_within_60d") is True)

        # 3: new true-relist, БЕЗ merge (сырой property_id).
        raw_props: dict[int, list[dict]] = defaultdict(list)
        for l in group:
            pid = l.get("property_id")
            if pid is not None:
                raw_props[pid].append(l)
        new_true_relist_before = sum(1 for members in raw_props.values() if len(members) > 1)
        unique_properties_before = len(raw_props)

        # 4: new true-relist, С merge (canonical property_id).
        canonical_props: dict[int, list[dict]] = defaultdict(list)
        for l in group:
            pid = l.get("property_id")
            if pid is not None:
                canonical_props[merge_map.get(pid, pid)].append(l)
        new_true_relist_after = sum(1 for members in canonical_props.values() if len(members) > 1)
        unique_properties_after = len(canonical_props)

        touched_by_merge = any(l.get("property_id") in merge_map for l in group)
        is_ambiguous = total > _AMBIGUOUS_NAME_MIN_LISTINGS

        rows.append({
            "seller_name": name_norm, "total_listings": total,
            "old_formula_before": old_relist, "old_formula_after": old_relist,  # тождественно, см. докстринг
            "new_true_relist_before": new_true_relist_before, "new_true_relist_after": new_true_relist_after,
            "unique_properties_before": unique_properties_before, "unique_properties_after": unique_properties_after,
            "touched_by_merge": touched_by_merge, "is_ambiguous": is_ambiguous,
        })

    all_sellers_with_property_data = [r for r in rows if r["unique_properties_before"] > 0]
    touched = [r for r in rows if r["touched_by_merge"]]

    def _agg(subset, key):
        return sum(r[key] for r in subset)

    print(f"\n=== ГЛОБАЛЬНЫЙ эффект (все {len(all_sellers_with_property_data)} продавцов "
          f"с хоть одним property_id) ===")
    print(f"  old_formula (before==after, НЕ смотрит на property_id): "
          f"{_agg(all_sellers_with_property_data, 'old_formula_before')}")
    print(f"  new_true_relist BEFORE merge: {_agg(all_sellers_with_property_data, 'new_true_relist_before')}")
    print(f"  new_true_relist AFTER merge:  {_agg(all_sellers_with_property_data, 'new_true_relist_after')}")
    print(f"  unique_properties BEFORE:     {_agg(all_sellers_with_property_data, 'unique_properties_before')}")
    print(f"  unique_properties AFTER:      {_agg(all_sellers_with_property_data, 'unique_properties_after')}")

    print(f"\n=== Эффект ТОЛЬКО среди {len(touched)} продавцов, затронутых хотя бы одним merge ===")
    print(f"  old_formula (before==after):  {_agg(touched, 'old_formula_before')}")
    print(f"  new_true_relist BEFORE merge: {_agg(touched, 'new_true_relist_before')}")
    print(f"  new_true_relist AFTER merge:  {_agg(touched, 'new_true_relist_after')}")
    print(f"  unique_properties BEFORE:     {_agg(touched, 'unique_properties_before')}")
    print(f"  unique_properties AFTER:      {_agg(touched, 'unique_properties_after')}")

    delta_relist = _agg(touched, 'new_true_relist_after') - _agg(touched, 'new_true_relist_before')
    delta_unique = _agg(touched, 'unique_properties_before') - _agg(touched, 'unique_properties_after')
    print(f"\n  Δ true_relist (after-before): {delta_relist:+d} "
          f"(положительное = relist РАСТЁТ после merge — несколько листингов ОДНОГО продавца, "
          f"ранее считавшихся разными properties, теперь на одной canonical property)")
    print(f"  Δ unique_properties (before-after, положительное=падает после merge): {delta_unique}")
    print(f"  Почему изменение маленькое относительно 70 компонент/101 рёбер: "
          f"{n_multi_agent}/{seller_profile_scope_components} компонент — ЧИСТО multi-agent "
          f"(разные продавцы, каждый по-прежнему видит РОВНО 1 свой листинг на этой property "
          f"после merge — их личный unique_properties/true_relist НЕ меняется), только "
          f"{n_true_relist + n_both} компонент реально содержат >1 листинг ОДНОГО продавца.")

    # ── Ambiguous names (не изменилось содержательно) ──────────────────────
    ambiguous_rows = sorted([r for r in rows if r["is_ambiguous"]], key=lambda r: r["total_listings"], reverse=True)
    print(f"\n=== Ambiguous имена (>{_AMBIGUOUS_NAME_MIN_LISTINGS} объявлений): {len(ambiguous_rows)} ===")
    print("НЕ считать доказанной единой идентичностью — merge подтверждает КВАРТИРУ, не ЧЕЛОВЕКА.")
    for r in ambiguous_rows[:10]:
        div = round(r["unique_properties_after"] / r["total_listings"], 3) if r["total_listings"] else None
        print(f"  {r['seller_name']!r}: listings={r['total_listings']}, "
              f"unique_properties_after={r['unique_properties_after']}, diversity={div}")


if __name__ == "__main__":
    asyncio.run(main())
