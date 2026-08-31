#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/audit_bigville_naming_pattern.py — задача 2026-08-30,
"Complex Identity layer", шаг 2: отдельный read-only разбор паттерна
"Бигвилль X" vs "X", найденного в audit/complex-sibling-phase-duplicate-
resolution как систематически неверно классифицированный suffix-only
эвристикой (sibling_phase вместо вероятного duplicate/renamed).

НЕ хардкодит вывод заранее — гипотеза ("Бигвилль X" ≈ marketing/reseller
naming variant, не отдельная фаза) ПРОВЕРЯЕТСЯ по фактическим данным
(overlapping listings/properties через Property Identity, а не просто
"похожие названия рядом"), не принимается на веру. Read-only, ни одной
записи в БД.

    venv/bin/python scripts/audit_bigville_naming_pattern.py
"""
from __future__ import annotations

import asyncio
import difflib
import json
import os
import re
import sys
from collections import Counter
from math import atan2, cos, radians, sin, sqrt

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

_BIGVILLE_PREFIX_RE = re.compile(r"^\s*бигвилл[ья]?\s+", re.IGNORECASE)
_BIGVILLE_PAREN_RE = re.compile(r"\(\s*бигвилл[ья]?\s+(.+?)\)", re.IGNORECASE)


def _norm(s: str) -> str:
    return re.sub(r"[\s.]+", "", s.strip().lower())


def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    R = 6371000.0
    p1, p2 = radians(lat1), radians(lat2)
    dphi, dlmb = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(p1) * cos(p2) * sin(dlmb / 2) ** 2
    return 2 * R * atan2(sqrt(a), sqrt(1 - a))


def _name_sim(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()


async def main() -> None:
    from bot.db.pg import close_pool, fetch, init_pool
    await init_pool(DATABASE_URL)
    try:
        await run(fetch)
    finally:
        await close_pool()


async def run(fetch) -> None:
    print("=" * 78)
    print('"Бигвилль X" vs "X" — read-only naming-pattern audit')
    print("=" * 78)

    complexes = await fetch(
        "SELECT id, name, address, lat, lon, year_built, developer, developer_id "
        "FROM complexes WHERE COALESCE(is_garbage, FALSE) = FALSE"
    )
    complexes = [dict(r) for r in complexes]
    by_id = {c["id"]: c for c in complexes}
    by_norm: dict[str, list[int]] = {}
    for c in complexes:
        by_norm.setdefault(_norm(c["name"]), []).append(c["id"])

    bigville = [c for c in complexes if _BIGVILLE_PREFIX_RE.match(c["name"]) or _BIGVILLE_PAREN_RE.search(c["name"])]
    print(f"\ncomplexes matching a 'Бигвилль' naming pattern: {len(bigville)}")

    pairs = []
    unmatched = []
    for c in bigville:
        paren = _BIGVILLE_PAREN_RE.search(c["name"])
        if paren:
            stripped = paren.group(1)
        else:
            stripped = _BIGVILLE_PREFIX_RE.sub("", c["name"])
        stripped = stripped.strip()

        exact_ids = [i for i in by_norm.get(_norm(stripped), []) if i != c["id"]]
        if exact_ids:
            pairs.append((c["id"], exact_ids[0], "exact_stripped_match"))
            continue

        # fuzzy fallback — best name_sim among all other non-Бигвилль complexes
        best_id, best_sim = None, 0.0
        for c2 in complexes:
            if c2["id"] == c["id"] or c2 in bigville:
                continue
            sim = _name_sim(stripped, c2["name"])
            if sim > best_sim:
                best_sim, best_id = sim, c2["id"]
        if best_id is not None and best_sim >= 0.6:
            pairs.append((c["id"], best_id, f"fuzzy_match(sim={best_sim:.2f})"))
        else:
            unmatched.append((c["id"], c["name"], stripped, best_id, best_sim))

    print(f"pairs found (Бигвилль complex has a counterpart): {len(pairs)}")
    print(f"Бигвилль complexes with NO counterpart found: {len(unmatched)}")
    for cid, name, stripped, best_id, best_sim in unmatched:
        print(f"  id={cid} name={name!r} stripped={stripped!r} best_guess={best_id} sim={best_sim:.2f}")

    # ── deep per-pair evidence ────────────────────────────────────────
    exact_listing_names = await fetch(
        "SELECT al.id AS listing_id, al.complex_name, al.address, al.lat, al.lon, al.is_active "
        "FROM apartment_listings al WHERE al.complex_name IS NOT NULL"
    )
    listings_by_norm_complex_name: dict[str, list[dict]] = {}
    for r in exact_listing_names:
        listings_by_norm_complex_name.setdefault(_norm(r["complex_name"]), []).append(dict(r))

    prop_rows = await fetch(
        "SELECT pl.listing_id, pl.property_id, p.complex_id FROM property_listings pl "
        "JOIN properties p ON p.property_id = pl.property_id"
    )
    property_of_listing = {r["listing_id"]: r["property_id"] for r in prop_rows}
    complex_of_property = {r["listing_id"]: r["complex_id"] for r in prop_rows}

    results = []
    relation_counts: Counter[str] = Counter()

    for a_id, b_id, method in pairs:
        a, b = by_id[a_id], by_id[b_id]
        listings_a = listings_by_norm_complex_name.get(_norm(a["name"]), [])
        listings_b = listings_by_norm_complex_name.get(_norm(b["name"]), [])
        dist_m = _haversine_m(a["lat"], a["lon"], b["lat"], b["lon"]) if a["lat"] and b["lat"] else None
        same_dev = a.get("developer_id") is not None and a["developer_id"] == b["developer_id"]
        year_diff = abs(a["year_built"] - b["year_built"]) if a.get("year_built") and b.get("year_built") else None

        # "same physical building" evidence: do listings from A and B
        # resolve (via Property Identity) to properties SHARING an
        # address_hash / floor+area combination? Cheaper honest proxy
        # available now: do any properties linked to A-side listings
        # ALSO have a listing whose complex_name is B (i.e. one physical
        # property observed under BOTH names over time)? This is the
        # only evidence that would be IMPOSSIBLE for a genuine sibling/
        # different building (those never share a property_id).
        props_a = {property_of_listing[l["listing_id"]] for l in listings_a if l["listing_id"] in property_of_listing}
        props_b = {property_of_listing[l["listing_id"]] for l in listings_b if l["listing_id"] in property_of_listing}
        shared_properties = props_a & props_b

        existing_complex_ids_a = {complex_of_property[l["listing_id"]] for l in listings_a
                                   if l["listing_id"] in complex_of_property and complex_of_property[l["listing_id"]] is not None}
        existing_complex_ids_b = {complex_of_property[l["listing_id"]] for l in listings_b
                                   if l["listing_id"] in complex_of_property and complex_of_property[l["listing_id"]] is not None}
        conflicting_assignment = bool((existing_complex_ids_a | existing_complex_ids_b) - {a_id, b_id})

        # verdict, NOT hardcoded ahead of time — computed from the actual
        # evidence gathered above, printed with the evidence so it's
        # checkable, not asserted.
        if shared_properties:
            # Одна и та же физическая квартира (Property Identity) НАБЛЮДАЛАСЬ
            # под ОБОИМИ названиями — невозможно для настоящей sibling/
            # соседней записи (у той были бы свои отдельные properties).
            relation = "duplicate_same_complex"
        elif dist_m is not None and dist_m <= 60 and same_dev and (year_diff is None or year_diff <= 1):
            # Координаты почти совпадают (<=60м) И тот же застройщик И год
            # близко — сильная косвенная корроборация "тот же физический
            # объект", но без ПРЯМОГО property-пересечения (ещё не
            # накопилось релистов под обоими именами) -> renamed_same_
            # complex, не duplicate (нет прямого доказательства, только
            # косвенное — задача явно просит различать эти два).
            relation = "renamed_same_complex"
        elif dist_m is not None and dist_m <= 200:
            relation = "ambiguous"
        else:
            relation = "separate_neighbor_complex"

        relation_counts[relation] += 1
        results.append({
            "bigville_id": a_id, "bigville_name": a["name"], "plain_id": b_id, "plain_name": b["name"],
            "match_method": method, "dist_m": dist_m, "same_developer": same_dev, "year_diff": year_diff,
            "listings_bigville": len(listings_a), "listings_plain": len(listings_b),
            "properties_bigville": len(props_a), "properties_plain": len(props_b),
            "shared_properties": len(shared_properties),
            "conflicting_existing_assignment": conflicting_assignment,
            "existing_complex_ids_seen": sorted(existing_complex_ids_a | existing_complex_ids_b),
            "relation": relation,
        })

    results.sort(key=lambda r: r["listings_bigville"] + r["listings_plain"], reverse=True)

    print("\n--- per-pair evidence (all pairs) ---")
    for r in results:
        print(f"\n  [{r['bigville_id']}] {r['bigville_name']!r}  <->  [{r['plain_id']}] {r['plain_name']!r}")
        print(f"    method={r['match_method']}  dist={r['dist_m']}m  same_dev={r['same_developer']}  "
              f"year_diff={r['year_diff']}")
        print(f"    listings: bigville={r['listings_bigville']} plain={r['listings_plain']}  "
              f"properties: bigville={r['properties_bigville']} plain={r['properties_plain']}  "
              f"SHARED properties (same physical unit under both names)={r['shared_properties']}")
        print(f"    existing properties.complex_id values seen among these listings: {r['existing_complex_ids_seen']}  "
              f"conflicting_with_pair={r['conflicting_existing_assignment']}")
        print(f"    => relation = {r['relation']}")

    print("\n--- summary ---")
    print(f"total Бигвилль complexes examined: {len(bigville)}")
    print(f"pairs found: {len(pairs)}")
    for rel, n in relation_counts.most_common():
        print(f"  {rel:26s} {n}")
    total_listings_fixed = sum(r["listings_bigville"] + r["listings_plain"] for r in results
                                if r["relation"] in ("duplicate_same_complex", "renamed_same_complex"))
    total_properties_fixed = sum(r["properties_bigville"] + r["properties_plain"] for r in results
                                  if r["relation"] in ("duplicate_same_complex", "renamed_same_complex"))
    print(f"\nlistings potentially resolvable if duplicate_same_complex/renamed_same_complex pairs "
          f"confirmed by human review: {total_listings_fixed}")
    print(f"properties potentially resolvable: {total_properties_fixed}")

    out_path = os.path.join(os.path.dirname(__file__), "..", "bigville_pattern_audit.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)
    print(f"\nfull machine-readable dump written to {os.path.abspath(out_path)} (NOT committed — local artifact)")

    print("\n" + "=" * 78)
    print("НИЧЕГО не записано в БД — этот скрипт read-only от начала до конца.")
    print("=" * 78)


if __name__ == "__main__":
    asyncio.run(main())
