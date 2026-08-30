#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/audit_complex_resolution_coverage.py — задача 2026-08-30,
"Complex Identity / resolved linkage coverage". Read-only end-to-end (одни
SELECT'ы, ни одной записи) — Phase 1-5 аудит того, насколько безопасно
увеличить deterministic linkage listing/property -> canonical complex_id
(bottleneck: complex_name ~88% listings, resolved_house_id ~1.35%,
properties.complex_id ~69.6%).

Классифицирует КАЖДЫЙ apartment_listings с complex_name IS NOT NULL И
resolved_house_id IS NULL (текущий "нерезолвленный" остаток) по классам
уверенности (Phase 1), затем считает quality-audit (Phase 2, house
number/geo/year_built agreement — на ВСЕЙ выборке, не на sample, размер
позволяет), затем применяет Tier A/B/C операционное определение (Phase 3)
и симулирует impact backfill'а ТОЛЬКО Tier A на get_complex_market_profile
(Phase 5) — ничего не пишет, только считает "что было бы".

    venv/bin/python scripts/audit_complex_resolution_coverage.py
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

_SOFT_PREFIX_RE = re.compile(r"^\s*(жк|кг)\.?\s+", re.IGNORECASE)
_HOUSE_NUMBER_RE = re.compile(r"(\d+[а-яa-z]?(?:/\d+)?)\s*$", re.IGNORECASE)
_GEO_NEAR_M = 150.0
_GRID_DEG = 0.0025  # ~250м на широте Астаны — ячейка сетки для geo pre-filter


def _norm_exact(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip()).lower()


def _norm_soft(s: str) -> str:
    return _norm_exact(_SOFT_PREFIX_RE.sub("", s.strip()))


def _house_number(address: str | None) -> str | None:
    if not address:
        return None
    m = _HOUSE_NUMBER_RE.search(address.strip())
    return m.group(1).lower() if m else None


def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    from math import atan2, cos, radians, sin, sqrt
    R = 6371000.0
    p1, p2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlmb = radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(p1) * cos(p2) * sin(dlmb / 2) ** 2
    return 2 * R * atan2(sqrt(a), sqrt(1 - a))


def _grid_key(lat: float, lon: float) -> tuple[int, int]:
    return (int(lat / _GRID_DEG), int(lon / _GRID_DEG))


async def main() -> None:
    from bot.db.pg import close_pool, fetch, init_pool

    await init_pool(DATABASE_URL)
    try:
        await run()
    finally:
        await close_pool()


async def run() -> None:
    from bot.db.pg import fetch

    print("=" * 78)
    print("Complex Identity / resolved linkage coverage — read-only audit")
    print("=" * 78)

    complexes = await fetch(
        "SELECT id, name, address, lat, lon, year_built, developer, developer_id "
        "FROM complexes WHERE COALESCE(is_garbage, FALSE) = FALSE"
    )
    complexes = [dict(r) for r in complexes]
    total_complexes = await fetch("SELECT count(*) AS n FROM complexes")
    garbage_complexes = await fetch("SELECT count(*) AS n FROM complexes WHERE is_garbage")
    print(f"\ncomplexes total: {total_complexes[0]['n']}  garbage: {garbage_complexes[0]['n']}  "
          f"clean (used below): {len(complexes)}")

    exact_index: dict[str, list[int]] = defaultdict(list)
    soft_index: dict[str, list[int]] = defaultdict(list)
    by_id: dict[int, dict] = {}
    geo_grid: dict[tuple[int, int], list[int]] = defaultdict(list)
    for c in complexes:
        by_id[c["id"]] = c
        exact_index[_norm_exact(c["name"])].append(c["id"])
        soft_index[_norm_soft(c["name"])].append(c["id"])
        if c["lat"] is not None and c["lon"] is not None:
            geo_grid[_grid_key(c["lat"], c["lon"])].append(c["id"])

    listings = await fetch(
        """
        SELECT al.id, al.complex_name, al.address, al.lat, al.lon, al.year_built,
               al.is_active, al.first_seen
        FROM apartment_listings al
        WHERE al.complex_name IS NOT NULL AND al.complex_name <> ''
          AND al.resolved_house_id IS NULL
          AND COALESCE(al.is_duplicate, FALSE) = FALSE
        """
    )
    listings = [dict(r) for r in listings]
    print(f"\ntarget listings (complex_name set, resolved_house_id NULL, not duplicate): {len(listings)}")

    # ── existing properties.complex_id, для conflict-детекции ────────────
    prop_rows = await fetch(
        """
        SELECT pl.listing_id, p.complex_id
        FROM property_listings pl
        JOIN properties p ON p.property_id = pl.property_id
        """
    )
    existing_property_complex: dict[str, int | None] = {r["listing_id"]: r["complex_id"] for r in prop_rows}

    # ── классификация ─────────────────────────────────────────────────
    classes: Counter[str] = Counter()
    class_listings: dict[str, list[dict]] = defaultdict(list)
    ambiguous_candidate_sizes: list[int] = []
    conflicts: list[dict] = []
    no_alias_note = "aliases: complexes НЕ имеет alias-таблицы/колонки на 2026-08-30 " \
                     "(developers.aliases существует, complexes — нет) — класс 'exact alias match' = 0 структурно."

    for l in listings:
        exact_ids = sorted(set(exact_index.get(_norm_exact(l["complex_name"]), [])))
        soft_ids = sorted(set(soft_index.get(_norm_soft(l["complex_name"]), [])))
        geo_ids: list[int] = []
        if l["lat"] is not None and l["lon"] is not None:
            gk = _grid_key(l["lat"], l["lon"])
            seen = set()
            for dq in (-1, 0, 1):
                for dr in (-1, 0, 1):
                    for cid in geo_grid.get((gk[0] + dq, gk[1] + dr), []):
                        if cid in seen:
                            continue
                        seen.add(cid)
                        c = by_id[cid]
                        if _haversine_m(l["lat"], l["lon"], c["lat"], c["lon"]) <= _GEO_NEAR_M:
                            geo_ids.append(cid)

        if exact_ids:
            cls = "exact_canonical_unique" if len(exact_ids) == 1 else "exact_canonical_ambiguous"
            candidates = exact_ids
        elif soft_ids:
            cls = "normalized_unique" if len(soft_ids) == 1 else "normalized_ambiguous"
            candidates = soft_ids
        elif geo_ids:
            cls = "geo_only_match" if len(geo_ids) == 1 else "geo_only_ambiguous"
            candidates = geo_ids
        else:
            cls = "no_candidate"
            candidates = []

        classes[cls] += 1
        if len(candidates) > 1:
            ambiguous_candidate_sizes.append(len(candidates))

        rec = {"listing": l, "candidates": candidates, "class": cls}
        class_listings[cls].append(rec)

        existing_cid = existing_property_complex.get(l["id"])
        if existing_cid is not None and candidates and existing_cid not in candidates:
            conflicts.append({
                "listing_id": l["id"], "class": cls, "existing_property_complex_id": existing_cid,
                "candidates": candidates,
            })

    print("\n--- Phase 1: classification ---")
    for cls, n in classes.most_common():
        print(f"  {cls:30s} {n:6d}  ({n / len(listings) * 100:.1f}%)")
    print(f"\n  {no_alias_note}")
    print(f"\n  ambiguous candidate-set sizes: min={min(ambiguous_candidate_sizes) if ambiguous_candidate_sizes else 0} "
          f"max={max(ambiguous_candidate_sizes) if ambiguous_candidate_sizes else 0} "
          f"median={sorted(ambiguous_candidate_sizes)[len(ambiguous_candidate_sizes)//2] if ambiguous_candidate_sizes else 0}")
    print(f"\n  conflicts (existing properties.complex_id disagrees with candidate(s)): {len(conflicts)}")
    for c in conflicts[:10]:
        print(f"    listing={c['listing_id']} class={c['class']} "
              f"existing_property_complex_id={c['existing_property_complex_id']} candidates={c['candidates']}")
    if len(conflicts) > 10:
        print(f"    ... and {len(conflicts) - 10} more")

    # ── Phase 2: quality audit — house number / geo / year_built agreement,
    # на ВСЕЙ выборке каждого класса, не sample (размер позволяет) ─────────
    print("\n--- Phase 2: quality audit (full population per class, not sampled) ---")
    same_norm_multi = defaultdict(set)
    for norm, ids in soft_index.items():
        if len(ids) > 1:
            same_norm_multi[norm] = set(ids)
    print(f"\n  'same normalized name -> multiple complexes' groups: {len(same_norm_multi)}")
    shown = 0
    for norm, ids in same_norm_multi.items():
        if shown >= 8:
            break
        names = [by_id[i]["name"] for i in ids]
        print(f"    {norm!r} -> complex_ids={sorted(ids)} names={names}")
        shown += 1

    for cls in ("exact_canonical_unique", "normalized_unique"):
        recs = class_listings.get(cls, [])
        if not recs:
            continue
        n = len(recs)
        hn_agree = hn_total = 0
        geo_agree = geo_total = 0
        year_agree = year_total = 0
        dev_known_on_complex = 0
        for r in recs:
            l, cid = r["listing"], r["candidates"][0]
            c = by_id[cid]
            lhn, chn = _house_number(l["address"]), _house_number(c.get("address"))
            if lhn and chn:
                hn_total += 1
                if lhn == chn:
                    hn_agree += 1
            if l["lat"] is not None and l["lon"] is not None and c["lat"] is not None and c["lon"] is not None:
                geo_total += 1
                if _haversine_m(l["lat"], l["lon"], c["lat"], c["lon"]) <= _GEO_NEAR_M:
                    geo_agree += 1
            if l.get("year_built") and c.get("year_built"):
                year_total += 1
                if l["year_built"] == c["year_built"]:
                    year_agree += 1
            if c.get("developer_id") is not None or c.get("developer"):
                dev_known_on_complex += 1
        print(f"\n  class={cls} (n={n})")
        print(f"    house_number agreement: {hn_agree}/{hn_total} checkable "
              f"({hn_agree/hn_total*100:.1f}%)" if hn_total else "    house_number agreement: 0 checkable (address missing on one side)")
        print(f"    geo<=%.0fm agreement: %d/%d checkable (%.1f%%)" % (
            _GEO_NEAR_M, geo_agree, geo_total, (geo_agree/geo_total*100 if geo_total else 0)))
        print(f"    year_built agreement: {year_agree}/{year_total} checkable "
              f"({year_agree/year_total*100:.1f}% )" if year_total else "    year_built agreement: 0 checkable")
        print(f"    complex has developer known: {dev_known_on_complex}/{n} ({dev_known_on_complex/n*100:.1f}%)")

    # ── Phase 3/5: Tier A operational definition + impact simulation ──────
    # Tier A (операционально, на ЭТИХ данных — 'explicit house/complex id'
    # эквивалента нет вне resolved_house_id САМОГО, alias-таблицы нет):
    # exact_canonical_unique И (house_number agreement ИЛИ адрес недоступен
    # на одной из сторон, НО тогда geo<=150m должен согласиться) И нет
    # конфликта с уже существующим properties.complex_id.
    print("\n--- Phase 3/5: Tier A operational definition + impact simulation ---")
    tier_a: list[dict] = []
    tier_a_no_conflict_check = {c["listing_id"] for c in conflicts}
    for r in class_listings.get("exact_canonical_unique", []):
        l, cid = r["listing"], r["candidates"][0]
        c = by_id[cid]
        lhn, chn = _house_number(l["address"]), _house_number(c.get("address"))
        geo_ok = None
        if l["lat"] is not None and l["lon"] is not None and c["lat"] is not None and c["lon"] is not None:
            geo_ok = _haversine_m(l["lat"], l["lon"], c["lat"], c["lon"]) <= _GEO_NEAR_M

        if lhn and chn:
            addr_ok = lhn == chn
        elif geo_ok is not None:
            addr_ok = geo_ok
        else:
            addr_ok = False  # ни адреса, ни гео — недостаточно для Tier A

        if addr_ok and l["id"] not in tier_a_no_conflict_check:
            tier_a.append({"listing_id": l["id"], "complex_id": cid, "property_complex_id": existing_property_complex.get(l["id"])})

    print(f"\n  Tier A listings (exact unique name + address/geo agreement + no conflict): {len(tier_a)} "
          f"/ {len(class_listings.get('exact_canonical_unique', []))} exact_canonical_unique "
          f"({len(class_listings.get('exact_canonical_unique', []))} total in that class)")

    # impact: new resolved_house_id coverage
    current_resolved = await fetch("SELECT count(*) AS n FROM apartment_listings WHERE resolved_house_id IS NOT NULL")
    current_resolved_n = current_resolved[0]["n"]
    print(f"\n  resolved_house_id coverage: {current_resolved_n} -> {current_resolved_n + len(tier_a)} "
          f"(+{len(tier_a)}) of {await _total_listings(fetch)} total listings")

    # impact on properties.complex_id: how many linked properties currently NULL would get filled
    tier_a_listing_ids = [t["listing_id"] for t in tier_a]
    props_affected = await fetch(
        "SELECT p.property_id, p.complex_id FROM property_listings pl "
        "JOIN properties p ON p.property_id = pl.property_id "
        "WHERE pl.listing_id = ANY($1::text[])", tier_a_listing_ids,
    ) if tier_a_listing_ids else []
    props_affected = [dict(r) for r in props_affected]
    props_null_now = sum(1 for p in props_affected if p["complex_id"] is None)
    print(f"\n  properties reachable via Tier A listings: {len(props_affected)} "
          f"(currently complex_id NULL: {props_null_now}, already set: {len(props_affected) - props_null_now})")

    # how many complexes cross the >=5 active-properties threshold
    active_props_now = await fetch(
        """
        SELECT p.complex_id, count(*) AS n
        FROM properties p JOIN property_listings pl ON pl.property_id = p.property_id
        JOIN apartment_listings al ON al.id = pl.listing_id AND al.is_active IS NOT FALSE
        WHERE p.complex_id IS NOT NULL
        GROUP BY p.complex_id
        """
    )
    active_props_now_map = {r["complex_id"]: r["n"] for r in active_props_now}
    delta_active: dict[int, int] = defaultdict(int)
    for t in tier_a:
        if t["property_complex_id"] is None:
            l = next(x["listing"] for x in class_listings["exact_canonical_unique"] if x["listing"]["id"] == t["listing_id"])
            if l["is_active"] is not False:
                delta_active[t["complex_id"]] += 1

    before_sufficient = {cid for cid, n in active_props_now_map.items() if n >= 5}
    after_map = dict(active_props_now_map)
    for cid, d in delta_active.items():
        after_map[cid] = after_map.get(cid, 0) + d
    after_sufficient = {cid for cid, n in after_map.items() if n >= 5}
    newly_sufficient = after_sufficient - before_sufficient

    print(f"\n  complexes with >=5 active properties: {len(before_sufficient)} -> {len(after_sufficient)} "
          f"(+{len(newly_sufficient)} newly sufficient)")
    print(f"  complexes touched by Tier A backfill at all: {len(delta_active)}")

    print("\n" + "=" * 78)
    print("НИЧЕГО не записано в БД — этот скрипт read-only от начала до конца.")
    print("=" * 78)


async def _total_listings(fetch) -> int:
    r = await fetch("SELECT count(*) AS n FROM apartment_listings")
    return r[0]["n"]


if __name__ == "__main__":
    asyncio.run(main())
