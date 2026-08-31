#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/build_complex_relation_review_dataset.py — задача 2026-08-30,
"Complex Identity layer", шаг 3: top-100 human-review dataset. Read-only,
пишет ТОЛЬКО в локальный JSON-файл (не в БД) — задача явно: "Не создавать
UI пока. JSON/CSV/report достаточно."

Строит candidate pairs как audit/complex-sibling-phase-duplicate-
resolution, НО с исправлением, найденным в этом же заходе (см. audit_
bigville_naming_pattern.py) — normalize снимает и PREFIX (не только
suffix): "Бигвилль X"/"X" больше не проваливается мимо root-match.
Классификация — 6 меток, включая различение duplicate_same_complex
(прямое property-пересечение через Property Identity) от renamed_same_
complex (сильная косвенная корроборация: координаты+застройщик+год, БЕЗ
прямого пересечения) — задача явно просила это разделение.

`ambiguous` НЕ пишется как relation_type в будущую complex_relations
(Phase 4 отчёта) — здесь это просто одна из candidate_relation меток в
review-датасете, human reviewer решает.

    venv/bin/python scripts/build_complex_relation_review_dataset.py
"""
from __future__ import annotations

import asyncio
import difflib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from math import atan2, cos, radians, sin, sqrt

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

_SOFT_PREFIX_RE = re.compile(r"^\s*(жк|кг|бигвилл[ья])\.?\s+", re.IGNORECASE)
_PHASE_SUFFIX_RE = re.compile(
    r"[\s\-\.,]+("
    r"(?:phase|фаза|очередь|оч\.?|корпус|корп\.?|блок|building|building\s*\d*|б\.?)\s*\d*"
    r"|[ivx]{1,4}"
    r"|к\.?\s*\d{1,2}"
    r"|\d{1,2}"
    r")\s*$",
    re.IGNORECASE,
)
_GEO_NEAR_M = 300.0
_GRID_DEG = 0.003
_TOP_N = 100


def _norm_exact(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip()).lower()


def _norm_soft(s: str) -> str:
    return _norm_exact(_SOFT_PREFIX_RE.sub("", s.strip()))


def _root_and_suffix(name: str) -> tuple[str, str | None]:
    soft = _norm_soft(name)
    m = _PHASE_SUFFIX_RE.search(soft)
    if m and len(soft) - len(m.group(0)) >= 2:
        return soft[: m.start()].strip(), m.group(1).strip()
    return soft, None


def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    R = 6371000.0
    p1, p2 = radians(lat1), radians(lat2)
    dphi, dlmb = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(p1) * cos(p2) * sin(dlmb / 2) ** 2
    return 2 * R * atan2(sqrt(a), sqrt(1 - a))


def _name_sim(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _norm_soft(a), _norm_soft(b)).ratio()


def _street_token(address: str | None) -> str | None:
    if not address:
        return None
    m = re.match(r"^(.*?),?\s*\d", address.strip())
    base = m.group(1) if m else address
    base = re.sub(r"[.,]", "", base).strip().lower()
    parts = base.split()
    return parts[-1] if parts else None


async def main() -> None:
    from bot.db.pg import close_pool, fetch, init_pool
    await init_pool(DATABASE_URL)
    try:
        await run(fetch)
    finally:
        await close_pool()


async def run(fetch) -> None:
    print("Building complex relation human-review dataset (read-only)...")

    complexes = await fetch(
        "SELECT id, name, address, lat, lon, year_built, developer, developer_id, "
        "parent_complex_id, is_umbrella FROM complexes WHERE COALESCE(is_garbage, FALSE) = FALSE"
    )
    complexes = [dict(r) for r in complexes]
    by_id = {c["id"]: c for c in complexes}

    exact_index: dict[str, list[int]] = defaultdict(list)
    for c in complexes:
        exact_index[_norm_exact(c["name"])].append(c["id"])

    prop_rows = await fetch(
        "SELECT pl.listing_id, pl.property_id, p.complex_id FROM property_listings pl "
        "JOIN properties p ON p.property_id = pl.property_id"
    )
    property_of_listing = {r["listing_id"]: r["property_id"] for r in prop_rows}
    complex_of_property = {r["listing_id"]: r["complex_id"] for r in prop_rows}

    listings = await fetch(
        "SELECT al.id, al.complex_name, al.address FROM apartment_listings al "
        "WHERE al.complex_name IS NOT NULL AND al.complex_name <> '' "
        "AND al.resolved_house_id IS NULL AND COALESCE(al.is_duplicate, FALSE) = FALSE"
    )
    listings_by_norm: dict[str, list[dict]] = defaultdict(list)
    for l in listings:
        listings_by_norm[_norm_exact(l["complex_name"])].append(dict(l))

    # ── candidate pairs: root-name groups (prefix+suffix-aware) + conflicts ──
    pairs: dict[tuple[int, int], set[str]] = defaultdict(set)

    root_groups: dict[str, list[int]] = defaultdict(list)
    suffix_by_id: dict[int, str | None] = {}
    for c in complexes:
        root, suf = _root_and_suffix(c["name"])
        suffix_by_id[c["id"]] = suf
        if root:
            root_groups[root].append(c["id"])
    for ids in root_groups.values():
        if len(ids) > 1:
            for i in range(len(ids)):
                for j in range(i + 1, len(ids)):
                    pairs[(min(ids[i], ids[j]), max(ids[i], ids[j]))].add("root_name")

    n_conflicts = 0
    for l in listings:
        cids = sorted(set(exact_index.get(_norm_exact(l["complex_name"]), [])))
        if len(cids) != 1:
            continue
        existing_cid = complex_of_property.get(l["id"])
        if existing_cid is None or existing_cid == cids[0] or existing_cid not in by_id:
            continue
        n_conflicts += 1
        pairs[(min(existing_cid, cids[0]), max(existing_cid, cids[0]))].add("conflict")

    grid: dict[tuple[int, int], list[int]] = defaultdict(list)
    for c in complexes:
        if c["lat"] is not None and c["lon"] is not None:
            grid[(int(c["lat"] / _GRID_DEG), int(c["lon"] / _GRID_DEG))].append(c["id"])
    for c in complexes:
        if c["lat"] is None:
            continue
        gk = (int(c["lat"] / _GRID_DEG), int(c["lon"] / _GRID_DEG))
        for dq in (-1, 0, 1):
            for dr in (-1, 0, 1):
                for cid2 in grid.get((gk[0] + dq, gk[1] + dr), []):
                    if cid2 <= c["id"]:
                        continue
                    key = (c["id"], cid2)
                    if key in pairs:
                        continue
                    c2 = by_id[cid2]
                    d = _haversine_m(c["lat"], c["lon"], c2["lat"], c2["lon"])
                    if d <= _GEO_NEAR_M and _name_sim(c["name"], c2["name"]) >= 0.5:
                        pairs[key].add("geo_name_similarity")

    print(f"conflicts recomputed (prefix+suffix-aware root normalization): {n_conflicts}")
    print(f"total candidate pairs: {len(pairs)}")

    # ── classify + build review records ─────────────────────────────────
    records = []
    for (a_id, b_id), sources in pairs.items():
        a, b = by_id[a_id], by_id[b_id]
        root_a, suf_a = _root_and_suffix(a["name"])
        root_b, suf_b = _root_and_suffix(b["name"])
        root_match = bool(root_a) and root_a == root_b
        dist_m = _haversine_m(a["lat"], a["lon"], b["lat"], b["lon"]) if a["lat"] and b["lat"] else None
        name_sim = _name_sim(a["name"], b["name"])
        same_dev = a.get("developer_id") is not None and a["developer_id"] == b["developer_id"]
        street_a, street_b = _street_token(a.get("address")), _street_token(b.get("address"))
        same_street = bool(street_a and street_b and street_a == street_b)
        year_diff = abs(a["year_built"] - b["year_built"]) if a.get("year_built") and b.get("year_built") else None

        listings_a = listings_by_norm.get(_norm_exact(a["name"]), [])
        listings_b = listings_by_norm.get(_norm_exact(b["name"]), [])
        props_a = {property_of_listing[l["id"]] for l in listings_a if l["id"] in property_of_listing}
        props_b = {property_of_listing[l["id"]] for l in listings_b if l["id"] in property_of_listing}
        shared_properties = props_a & props_b
        n_conflict_listings = 0
        for l in listings_a:
            existing = complex_of_property.get(l["id"])
            if existing is not None and existing in (a_id, b_id) and existing != a_id:
                n_conflict_listings += 1
        for l in listings_b:
            existing = complex_of_property.get(l["id"])
            if existing is not None and existing in (a_id, b_id) and existing != b_id:
                n_conflict_listings += 1

        evidence = {
            "same_developer": same_dev, "same_street": same_street, "street_a": street_a, "street_b": street_b,
            "dist_m": round(dist_m, 1) if dist_m is not None else None, "year_diff": year_diff,
            "name_similarity": round(name_sim, 3), "root_match": root_match,
            "suffix_a": suf_a, "suffix_b": suf_b, "shared_properties_count": len(shared_properties),
        }

        if a.get("parent_complex_id") == b_id or b.get("parent_complex_id") == a_id:
            relation = "same_umbrella_project"
        elif shared_properties:
            relation = "duplicate_same_complex"
        elif dist_m is not None and dist_m <= 60 and same_dev and (year_diff is None or year_diff <= 1):
            relation = "renamed_same_complex"
        elif root_match and (suf_a or suf_b):
            relation = "sibling_phase"
        elif root_match and not suf_a and not suf_b and dist_m is not None and dist_m <= 50:
            relation = "duplicate_same_complex"
        elif name_sim >= 0.55 and dist_m is not None and dist_m <= _GEO_NEAR_M and (same_dev or same_street):
            relation = "sibling_phase"
        elif dist_m is not None and dist_m > 2000 and name_sim < 0.6:
            relation = "separate_neighbor_complex"
        else:
            relation = "ambiguous"

        records.append({
            "complex_id_a": a_id, "complex_id_b": b_id,
            "name_a": a["name"], "name_b": b["name"],
            "root_a": root_a, "root_b": root_b,
            "developer_id_a": a.get("developer_id"), "developer_id_b": b.get("developer_id"),
            "developer_name_a": a.get("developer"), "developer_name_b": b.get("developer"),
            "lat_a": a["lat"], "lon_a": a["lon"], "lat_b": b["lat"], "lon_b": b["lon"],
            "distance_m": evidence["dist_m"],
            "address_a": a.get("address"), "address_b": b.get("address"),
            "year_built_a": a.get("year_built"), "year_built_b": b.get("year_built"),
            "listing_count_a": len(listings_a), "listing_count_b": len(listings_b),
            "property_count_a": len(props_a), "property_count_b": len(props_b),
            "shared_property_count": len(shared_properties),
            "existing_complex_id_assignments_seen": sorted({
                cid for l in (listings_a + listings_b)
                if (cid := complex_of_property.get(l["id"])) is not None
            }),
            "conflict_listing_count": n_conflict_listings,
            "candidate_relation": relation,
            "evidence_summary": evidence,
            "sources": sorted(sources),
        })

    records.sort(key=lambda r: (r["conflict_listing_count"], r["property_count_a"] + r["property_count_b"]), reverse=True)
    top = records[:_TOP_N]

    relation_counts = Counter(r["candidate_relation"] for r in records)
    print("\ncandidate_relation distribution across ALL candidate pairs:")
    for rel, n in relation_counts.most_common():
        print(f"  {rel:26s} {n}")

    top_relation_counts = Counter(r["candidate_relation"] for r in top)
    print(f"\ncandidate_relation distribution across TOP {_TOP_N}:")
    for rel, n in top_relation_counts.most_common():
        print(f"  {rel:26s} {n}")

    out_path = os.path.join(os.path.dirname(__file__), "..", "complex_relation_review_top100.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "generated_from": "scripts/build_complex_relation_review_dataset.py",
            "total_candidate_pairs": len(records),
            "labels_available_for_review": [
                "duplicate_same_complex", "sibling_phase", "same_umbrella_project",
                "renamed_same_complex", "separate_neighbor_complex", "ambiguous",
            ],
            "top_100": top,
        }, f, ensure_ascii=False, indent=2, default=str)
    print(f"\ntop {len(top)} records written to {os.path.abspath(out_path)} (NOT committed to git — local review artifact)")
    print("НИЧЕГО не записано в БД.")


if __name__ == "__main__":
    asyncio.run(main())
