#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/audit_merge_canonical_scoring_dry_run.py — задача 2026-08-18,
follow-up "Property Identity — calibration validation", п.6: заменяет
простое правило "активнее сейчас -> раньше first_seen -> меньший id"
(docs/property_merge_design.md §1, первая версия) на многофакторный
deterministic scoring, и печатает ПОЛНЫЙ read-only dry-run на реальной
самой длинной цепочке accepted-решений. НИЧЕГО не пишет, физический
merge НЕ выполняется — этот файл только СЧИТАЕТ и ПЕЧАТАЕТ план.

## Почему многофакторный scoring, не три правила

Задача, явно: "простого правила недостаточно". Три старых правила
теряют информацию — property с 1 активным листингом, но 10-летней
историей и полными атрибутами может быть содержательно надёжнее
"якоря", чем property с 2 активными листингами, но без этажа/координат
и однодневной историей. Ниже — взвешенная сумма нормализованных
0..1 суб-баллов на факторах, которые перечислила задача:

  completeness (25%)        — доля заполненных complex_id/floor/
                               area_sqm/rooms (0..1, 4 поля)
  address_consistency (15%) — согласие ТЕКУЩИХ (не замороженных на
                               bootstrap) адресов связанных listing'ов
                               между собой: 1.0, если все совпадают
  coords_presence (10%)     — есть ли хоть один связанный listing с
                               lat/lon (apartment_listings, НЕ properties
                               — координаты живут на листинге)
  history_duration (15%)    — (last_seen_at-first_seen_at), нормализовано
                               ОТНОСИТЕЛЬНО компоненты (самая долгая
                               история в группе = 1.0)
  listing_count (15%)       — count(property_listings), нормализовано
                               относительно компоненты
  conflict_absence (10%)    — 1/(1+n) конфликтных candidate-строк,
                               касающихся этой property (0 конфликтов = 1.0)
  freshness (10%)           — recency last_seen_at, нормализовано
                               относительно компоненты (самый свежий = 1.0)

Веса — ЭКСПЕРТНЫЕ, НЕ откалиброванные на исходе реальных merge (тот же
честный disclaimer, что docs/location_score_calibration_audit.md §2 —
не выдаю их за измеренную истину). Стабильный tie-break — меньший
property_id (последняя строка сортировки, детерминированно).

## Что печатает dry-run на каждую компоненту

- итоговый score каждой property (полный breakdown по суб-баллам);
- выбранный canonical + обоснование (не просто "он выиграл", а какие
  суб-баллы были решающими);
- какие property_listings были бы репойнтнуты (listing_id -> откуда куда);
- конфликты атрибутов между canonical и losing (разные floor/area/rooms/
  complex_id — если ТАКИЕ пары вообще существуют, это сигнал, что merge
  ДОЛЖЕН был бы получить ручное предупреждение, не тихий auto-merge);
- как выглядел бы rollback (moved_listing_ids snapshot).

Полная таблица — на ВСЕ компоненты (сейчас: см. вывод). Развёрнутый
per-listing dry-run — только на САМУЮ длинную цепочку (задача, явно:
"для реальной цепочки из 15 properties").
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

_WEIGHTS = {
    "completeness": 0.25, "address_consistency": 0.15, "coords_presence": 0.10,
    "history_duration": 0.15, "listing_count": 0.15, "conflict_absence": 0.10, "freshness": 0.10,
}
assert abs(sum(_WEIGHTS.values()) - 1.0) < 1e-9


def _build_components(edges: list[dict]) -> dict[int, set[int]]:
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

    groups: dict[int, set[int]] = defaultdict(set)
    all_props = {p for r in edges for p in (r["prop_a"], r["prop_b"]) if p is not None}
    for p in all_props:
        groups[find(p)].add(p)
    # re-key by an arbitrary stable representative (min id) — canonical
    # SELECTION happens later via scoring, this key is just a dict key.
    return {min(members): members for members in groups.values()}


async def _load_property_facts(prop_ids: list[int]) -> dict[int, dict]:
    from bot.db.pg import fetch

    props = await fetch(
        "SELECT property_id, complex_id, floor, area_sqm, rooms, first_seen_at, last_seen_at "
        "FROM properties WHERE property_id = ANY($1::int[])", prop_ids)
    listings = await fetch("""
        SELECT pl.property_id, al.id AS listing_id, al.address, al.lat, al.lon, al.is_active
        FROM property_listings pl JOIN apartment_listings al ON al.id = pl.listing_id
        WHERE pl.property_id = ANY($1::int[])
    """, prop_ids)
    conflicts = await fetch("""
        SELECT pl.property_id, count(*) AS n
        FROM property_match_candidates pmc
        JOIN property_listings pl ON pl.listing_id = pmc.listing_id
        WHERE pmc.conflict_reasons IS NOT NULL AND pl.property_id = ANY($1::int[])
        GROUP BY pl.property_id
        UNION ALL
        SELECT pmc.candidate_property_id AS property_id, count(*) AS n
        FROM property_match_candidates pmc
        WHERE pmc.conflict_reasons IS NOT NULL AND pmc.candidate_property_id = ANY($1::int[])
        GROUP BY pmc.candidate_property_id
    """, prop_ids)

    listings_by_prop: dict[int, list[dict]] = defaultdict(list)
    for l in listings:
        listings_by_prop[l["property_id"]].append(dict(l))
    conflict_n: dict[int, int] = defaultdict(int)
    for c in conflicts:
        conflict_n[c["property_id"]] += c["n"]

    facts = {}
    for p in props:
        p = dict(p)
        pid = p["property_id"]
        facts[pid] = {
            **p,
            "listings": listings_by_prop.get(pid, []),
            "n_conflicts": conflict_n.get(pid, 0),
        }
    return facts


def _score_component(members: set[int], facts: dict[int, dict]) -> list[dict]:
    member_facts = [facts[p] for p in members if p in facts]
    if not member_facts:
        return []

    durations = [(f["last_seen_at"] - f["first_seen_at"]).total_seconds() for f in member_facts]
    max_duration = max(durations) or 1.0
    counts = [len(f["listings"]) for f in member_facts]
    max_count = max(counts) or 1
    last_seens = [f["last_seen_at"] for f in member_facts]
    newest = max(last_seens)
    oldest = min(last_seens)
    freshness_span = (newest - oldest).total_seconds() or 1.0

    scored = []
    for f in member_facts:
        completeness = sum([
            f["complex_id"] is not None, f["floor"] is not None,
            f["area_sqm"] is not None, f["rooms"] is not None,
        ]) / 4.0

        addrs = {(l["address"] or "").strip().lower() for l in f["listings"] if l["address"]}
        address_consistency = 1.0 if len(addrs) <= 1 else 1.0 / len(addrs)

        coords_presence = 1.0 if any(l["lat"] is not None and l["lon"] is not None for l in f["listings"]) else 0.0

        duration = (f["last_seen_at"] - f["first_seen_at"]).total_seconds()
        history_duration = duration / max_duration if max_duration else 0.0

        listing_count = len(f["listings"]) / max_count if max_count else 0.0

        conflict_absence = 1.0 / (1 + f["n_conflicts"])

        freshness = (f["last_seen_at"] - oldest).total_seconds() / freshness_span if freshness_span else 1.0

        subscores = {
            "completeness": completeness, "address_consistency": address_consistency,
            "coords_presence": coords_presence, "history_duration": history_duration,
            "listing_count": listing_count, "conflict_absence": conflict_absence, "freshness": freshness,
        }
        total = sum(_WEIGHTS[k] * v for k, v in subscores.items())
        scored.append({"property_id": f["property_id"], "score": round(total, 4),
                        "subscores": {k: round(v, 3) for k, v in subscores.items()},
                        "n_listings": len(f["listings"]), "n_conflicts": f["n_conflicts"],
                        "complex_id": f["complex_id"], "floor": f["floor"], "area_sqm": f["area_sqm"],
                        "rooms": f["rooms"], "first_seen_at": f["first_seen_at"], "last_seen_at": f["last_seen_at"]})

    # Стабильный tie-break — меньший property_id ПОСЛЕДНИМ ключом сортировки.
    scored.sort(key=lambda s: (-s["score"], s["property_id"]))
    return scored


async def main() -> None:
    from bot.db.pg import init_pool, close_pool, fetch

    await init_pool(DATABASE_URL)
    try:
        edges = await fetch("""
            SELECT pl.property_id AS prop_a, pmc.candidate_property_id AS prop_b
            FROM property_match_candidates pmc
            JOIN property_listings pl ON pl.listing_id = pmc.listing_id
            WHERE pmc.status = 'accepted'
        """)
        components = _build_components(edges)
        all_prop_ids = [p for members in components.values() for p in members]
        facts = await _load_property_facts(all_prop_ids)
    finally:
        await close_pool()

    print(f"компонент: {len(components)}, properties: {len(all_prop_ids)}")
    sizes = sorted((len(m) for m in components.values()), reverse=True)
    print(f"размеры (топ 10): {sizes[:10]}")

    biggest_key = max(components, key=lambda k: len(components[k]))
    biggest = components[biggest_key]
    print(f"\n=== Самая длинная цепочка: {len(biggest)} properties: {sorted(biggest)} ===")

    scored = _score_component(biggest, facts)
    print("\n-- Scoring (отсортировано по убыванию, canonical = первая строка) --")
    for s in scored:
        print(f"  property_id={s['property_id']:>6}  score={s['score']:.4f}  "
              f"listings={s['n_listings']}  conflicts={s['n_conflicts']}  "
              f"floor={s['floor']}  area={s['area_sqm']}  rooms={s['rooms']}  "
              f"complex_id={s['complex_id']}  first_seen={s['first_seen_at']:%Y-%m-%d}  "
              f"last_seen={s['last_seen_at']:%Y-%m-%d}")
        print(f"      subscores: {s['subscores']}")

    canonical = scored[0]
    losing = scored[1:]
    print(f"\n>>> CANONICAL: property_id={canonical['property_id']} (score={canonical['score']:.4f})")
    winning_factor = max(canonical["subscores"], key=lambda k: _WEIGHTS[k] * canonical["subscores"][k])
    print(f"    Решающий фактор (наибольший вклад веса×суббалла): {winning_factor}")

    print(f"\n>>> {len(losing)} properties были бы помечены 'merged', их property_listings repointed:")
    for l in losing:
        pid = l["property_id"]
        listing_ids = [x["listing_id"] for x in facts[pid]["listings"]]
        print(f"  property_id={pid} (score={l['score']:.4f}) -> {len(listing_ids)} listing(s): {listing_ids}")

    print("\n>>> Конфликты атрибутов между canonical и losing properties:")
    c = facts[canonical["property_id"]]
    any_conflict = False
    for l in losing:
        f = facts[l["property_id"]]
        diffs = []
        if c["floor"] != f["floor"]:
            diffs.append(f"floor {c['floor']} vs {f['floor']}")
        if c["rooms"] != f["rooms"]:
            diffs.append(f"rooms {c['rooms']} vs {f['rooms']}")
        if c["complex_id"] != f["complex_id"]:
            diffs.append(f"complex_id {c['complex_id']} vs {f['complex_id']}")
        if c["area_sqm"] is not None and f["area_sqm"] is not None and abs(c["area_sqm"] - f["area_sqm"]) > 1.0:
            diffs.append(f"area {c['area_sqm']} vs {f['area_sqm']}")
        if diffs:
            any_conflict = True
            print(f"  canonical={canonical['property_id']} vs losing={f['property_id']}: {'; '.join(diffs)}")
    if not any_conflict:
        print("  ни одного конфликта floor/rooms/complex_id/area>1м² не найдено — согласованная группа.")

    print("\n>>> Rollback shape (append-only, если бы merge был выполнен):")
    print("  moved_listing_ids snapshot на каждую losing property (см. выше per-property список) "
          "-> при откате repoint обратно ТОЛЬКО этих listing_id, не всех текущих листингов canonical "
          "(см. docs/property_merge_design.md §5).")

    print("\n=== Полная таблица canonical-выбора по ВСЕМ компонентам (кратко) ===")
    for key, members in sorted(components.items(), key=lambda kv: -len(kv[1])):
        sc = _score_component(members, facts)
        if not sc:
            continue
        top = sc[0]
        print(f"  size={len(members):2d}  canonical={top['property_id']:>6}  score={top['score']:.3f}  "
              f"members={sorted(members)}")


if __name__ == "__main__":
    asyncio.run(main())
