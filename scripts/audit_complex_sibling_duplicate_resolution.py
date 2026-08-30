#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/audit_complex_sibling_duplicate_resolution.py — задача
2026-08-30, "Complex Identity: sibling / phase / duplicate resolution"
(следующий шаг после audit/complex-resolution-coverage, verdict B — не
Tier A auto-write, пока не разобраны sibling/duplicate коллизии).
Read-only end-to-end — ни одной записи в БД, только SELECT'ы. Не пишет
в complex_duplicate_candidates (существующую таблицу) — "расширить
только в read-only режиме" значит: та же ИДЕЯ (кандидат-пары ЖК), не
трогая саму таблицу, полный отчёт печатается, не персистится.

Phase 1 — генерация candidate pairs (несколько независимых сигналов,
объединённых, не единственного "координаты рядом -> дубль" — задача
явно: "не считать близость координат достаточным доказательством").
Phase 2 — rule-based (НЕ ML) классификация каждой пары.
Phase 3 — top-N review packets.
Phase 4 — проверка parent_complex_id/is_umbrella на достаточность.
Phase 6 — пересчёт Tier A impact с учётом классификации.

    venv/bin/python scripts/audit_complex_sibling_duplicate_resolution.py
"""
from __future__ import annotations

import asyncio
import difflib
import os
import re
import sys
from collections import Counter, defaultdict
from math import atan2, cos, radians, sin, sqrt

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

_SOFT_PREFIX_RE = re.compile(r"^\s*(жк|кг)\.?\s+", re.IGNORECASE)
# Суффикс фазы/очереди/корпуса — то, что задача явно перечислила:
# "2, II, Phase, очередь, корпус и аналоги". Захватывает ОДИН trailing
# токен (после разделителя пробел/точка/дефис), не режет root ещё раз.
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


def _norm_exact(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip()).lower()


def _norm_soft(s: str) -> str:
    return _norm_exact(_SOFT_PREFIX_RE.sub("", s.strip()))


def _root_and_suffix(name: str) -> tuple[str, str | None]:
    """(root, suffix_matched) — suffix_matched=None, если строка не
    заканчивается узнаваемым phase/corpus/queue-паттерном."""
    soft = _norm_soft(name)
    m = _PHASE_SUFFIX_RE.search(soft)
    if m and len(soft) - len(m.group(0)) >= 2:  # root не должен схлопнуться в ничто
        root = soft[: m.start()].strip()
        return root, m.group(1).strip()
    return soft, None


def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    R = 6371000.0
    p1, p2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlmb = radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(p1) * cos(p2) * sin(dlmb / 2) ** 2
    return 2 * R * atan2(sqrt(a), sqrt(1 - a))


def _name_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _norm_soft(a), _norm_soft(b)).ratio()


_STREET_RE = re.compile(r"^(.*?),?\s*\d")


def _street_token(address: str | None) -> str | None:
    if not address:
        return None
    m = _STREET_RE.match(address.strip())
    base = m.group(1) if m else address
    base = re.sub(r"[.,]", "", base).strip().lower()
    parts = base.split()
    return parts[-1] if parts else None


def classify_pair(a: dict, b: dict, *, root_match: bool, suffix_a, suffix_b,
                   dist_m: float | None, name_sim: float) -> tuple[str, list[str]]:
    """Rule-based, НЕ ML. Возвращает (relation, evidence_notes)."""
    evidence = []
    same_dev = (a.get("developer_id") is not None and a.get("developer_id") == b.get("developer_id"))
    street_a, street_b = _street_token(a.get("address")), _street_token(b.get("address"))
    same_street = bool(street_a and street_b and street_a == street_b)
    year_diff = None
    if a.get("year_built") and b.get("year_built"):
        year_diff = abs(a["year_built"] - b["year_built"])

    if same_dev:
        evidence.append("same_developer")
    if same_street:
        evidence.append(f"same_street({street_a})")
    if dist_m is not None:
        evidence.append(f"dist={dist_m:.0f}m")
    if year_diff is not None:
        evidence.append(f"year_diff={year_diff}")
    evidence.append(f"name_sim={name_sim:.2f}")

    if a.get("parent_complex_id") == b["id"] or b.get("parent_complex_id") == a["id"]:
        evidence.append("existing_parent_complex_id_link")
        return "same_umbrella_project", evidence

    if root_match and (suffix_a or suffix_b):
        evidence.append(f"phase_suffix(a={suffix_a!r}, b={suffix_b!r})")
        return "sibling_phase", evidence

    if root_match and not suffix_a and not suffix_b:
        # одинаковый root БЕЗ фазового суффикса с обеих сторон — либо
        # буквальный дубль записи, либо просто похожие названия у разных
        # ЖК (проверяем корроборацию, иначе -> ambiguous, не штампуем
        # "дубль" на одной близости имени).
        if dist_m is not None and dist_m <= _GEO_NEAR_M and (same_dev or same_street):
            return "duplicate_same_complex", evidence
        if dist_m is not None and dist_m <= 50:
            return "duplicate_same_complex", evidence
        return "ambiguous", evidence

    if name_sim >= 0.55 and dist_m is not None and dist_m <= _GEO_NEAR_M and (same_dev or same_street):
        return "sibling_phase", evidence

    if dist_m is not None and dist_m > 2000 and name_sim < 0.6:
        return "separate_neighbor_complex", evidence

    return "ambiguous", evidence


async def main() -> None:
    from bot.db.pg import close_pool, init_pool
    await init_pool(DATABASE_URL)
    try:
        await run()
    finally:
        await close_pool()


async def run() -> None:
    from bot.db.pg import fetch

    print("=" * 78)
    print("Complex Identity: sibling / phase / duplicate resolution — read-only audit")
    print("=" * 78)

    complexes = await fetch(
        "SELECT id, name, address, lat, lon, year_built, developer, developer_id, "
        "parent_complex_id, is_umbrella FROM complexes WHERE COALESCE(is_garbage, FALSE) = FALSE"
    )
    complexes = [dict(r) for r in complexes]
    by_id = {c["id"]: c for c in complexes}
    print(f"\nclean complexes: {len(complexes)}")

    # ── Phase 1: candidate generation ────────────────────────────────
    pairs: dict[tuple[int, int], dict] = {}

    def add_pair(a_id: int, b_id: int, source: str) -> None:
        key = (min(a_id, b_id), max(a_id, b_id))
        pairs.setdefault(key, {"sources": set()})["sources"].add(source)

    # (a) root-name groups (после снятия phase/corpus/queue-суффикса)
    root_groups: dict[str, list[int]] = defaultdict(list)
    suffix_by_id: dict[int, str | None] = {}
    for c in complexes:
        root, suf = _root_and_suffix(c["name"])
        suffix_by_id[c["id"]] = suf
        if root:
            root_groups[root].append(c["id"])
    root_group_pairs = 0
    for root, ids in root_groups.items():
        if len(ids) > 1:
            for i in range(len(ids)):
                for j in range(i + 1, len(ids)):
                    add_pair(ids[i], ids[j], "root_name")
                    root_group_pairs += 1
    print(f"\nPhase 1a: root-name groups with >1 complex: "
          f"{sum(1 for v in root_groups.values() if len(v) > 1)} groups, {root_group_pairs} pairs")

    # (b) 2603 conflicts from previous audit (recomputed here, same logic)
    exact_index: dict[str, list[int]] = defaultdict(list)
    for c in complexes:
        exact_index[_norm_exact(c["name"])].append(c["id"])
    prop_rows = await fetch(
        "SELECT pl.listing_id, p.complex_id FROM property_listings pl "
        "JOIN properties p ON p.property_id = pl.property_id"
    )
    existing_property_complex = {r["listing_id"]: r["complex_id"] for r in prop_rows}
    listings = await fetch(
        "SELECT al.id, al.complex_name FROM apartment_listings al "
        "WHERE al.complex_name IS NOT NULL AND al.complex_name <> '' "
        "AND al.resolved_house_id IS NULL AND COALESCE(al.is_duplicate, FALSE) = FALSE"
    )
    conflict_listing_ids: dict[tuple[int, int], list[str]] = defaultdict(list)
    n_conflicts = 0
    for l in listings:
        cids = sorted(set(exact_index.get(_norm_exact(l["complex_name"]), [])))
        if len(cids) != 1:
            continue
        existing_cid = existing_property_complex.get(l["id"])
        if existing_cid is None or existing_cid == cids[0] or existing_cid not in by_id:
            continue
        n_conflicts += 1
        add_pair(existing_cid, cids[0], "conflict")
        key = (min(existing_cid, cids[0]), max(existing_cid, cids[0]))
        conflict_listing_ids[key].append(l["id"])
    print(f"Phase 1b: conflicts recomputed = {n_conflicts} listings "
          f"(same as previous audit's 2603, sanity check)")

    # (c) geo-proximity + name-similarity, not already covered
    grid: dict[tuple[int, int], list[int]] = defaultdict(list)
    for c in complexes:
        if c["lat"] is not None and c["lon"] is not None:
            grid[(int(c["lat"] / _GRID_DEG), int(c["lon"] / _GRID_DEG))].append(c["id"])
    geo_pairs_added = 0
    for c in complexes:
        if c["lat"] is None or c["lon"] is None:
            continue
        gk = (int(c["lat"] / _GRID_DEG), int(c["lon"] / _GRID_DEG))
        seen_here = set()
        for dq in (-1, 0, 1):
            for dr in (-1, 0, 1):
                for cid2 in grid.get((gk[0] + dq, gk[1] + dr), []):
                    if cid2 <= c["id"] or cid2 in seen_here:
                        continue
                    seen_here.add(cid2)
                    key = (min(c["id"], cid2), max(c["id"], cid2))
                    if key in pairs:
                        continue
                    c2 = by_id[cid2]
                    d = _haversine_m(c["lat"], c["lon"], c2["lat"], c2["lon"])
                    if d <= _GEO_NEAR_M and _name_similarity(c["name"], c2["name"]) >= 0.5:
                        add_pair(c["id"], cid2, "geo_name_similarity")
                        geo_pairs_added += 1
    print(f"Phase 1c: geo+name-similarity pairs added: {geo_pairs_added}")

    # (d) existing complex_duplicate_candidates rows (merge in, read-only)
    existing_cdc = await fetch("SELECT complex_id_a, complex_id_b, status FROM complex_duplicate_candidates")
    for r in existing_cdc:
        if r["complex_id_a"] in by_id and r["complex_id_b"] in by_id:
            add_pair(r["complex_id_a"], r["complex_id_b"], f"existing_cdc:{r['status']}")
    print(f"Phase 1d: existing complex_duplicate_candidates merged in: {len(existing_cdc)} rows")

    print(f"\nTotal unique candidate pairs (union of all sources): {len(pairs)}")

    # ── Phase 2: classify ─────────────────────────────────────────────
    relation_counts: Counter[str] = Counter()
    pair_details: list[dict] = []
    for (a_id, b_id), info in pairs.items():
        a, b = by_id[a_id], by_id[b_id]
        root_a, suf_a = _root_and_suffix(a["name"])
        root_b, suf_b = _root_and_suffix(b["name"])
        root_match = root_a == root_b and bool(root_a)
        dist_m = _haversine_m(a["lat"], a["lon"], b["lat"], b["lon"]) if a["lat"] and b["lat"] else None
        name_sim = _name_similarity(a["name"], b["name"])
        relation, evidence = classify_pair(a, b, root_match=root_match, suffix_a=suf_a, suffix_b=suf_b,
                                            dist_m=dist_m, name_sim=name_sim)
        relation_counts[relation] += 1
        key = (a_id, b_id)
        n_conflict_listings = len(conflict_listing_ids.get(key, []))
        pair_details.append({
            "a": a, "b": b, "relation": relation, "evidence": evidence, "dist_m": dist_m,
            "name_sim": name_sim, "sources": info["sources"], "n_conflict_listings": n_conflict_listings,
        })

    print("\n--- Phase 2: classification ---")
    for rel, n in relation_counts.most_common():
        print(f"  {rel:26s} {n:5d}")

    # ── impact: listings/properties touched per pair ───────────────────
    listings_by_complex_name: dict[int, int] = defaultdict(int)
    for l in listings:
        for cid in exact_index.get(_norm_exact(l["complex_name"]), []):
            listings_by_complex_name[cid] += 1
    props_by_complex: dict[int, int] = defaultdict(int)
    prop_complex_rows = await fetch("SELECT complex_id, count(*) AS n FROM properties WHERE complex_id IS NOT NULL GROUP BY complex_id")
    for r in prop_complex_rows:
        props_by_complex[r["complex_id"]] = r["n"]

    for pd in pair_details:
        pd["listings_impact"] = listings_by_complex_name.get(pd["a"]["id"], 0) + listings_by_complex_name.get(pd["b"]["id"], 0)
        pd["properties_impact"] = props_by_complex.get(pd["a"]["id"], 0) + props_by_complex.get(pd["b"]["id"], 0)

    pair_details.sort(key=lambda pd: (pd["n_conflict_listings"], pd["properties_impact"]), reverse=True)

    # ── Phase 3: top review packets ─────────────────────────────────────
    print("\n--- Phase 3: top 30 most impactful pairs (conflict_listings desc, then properties_impact) ---")
    for pd in pair_details[:30]:
        a, b = pd["a"], pd["b"]
        print(f"\n  [{a['id']} vs {b['id']}] relation={pd['relation']}  "
              f"conflict_listings={pd['n_conflict_listings']}  properties_impact={pd['properties_impact']}  "
              f"listings_impact={pd['listings_impact']}")
        print(f"    A: {a['name']!r:35s} dev_id={a.get('developer_id')} year={a.get('year_built')} "
              f"addr={a.get('address')!r}")
        print(f"    B: {b['name']!r:35s} dev_id={b.get('developer_id')} year={b.get('year_built')} "
              f"addr={b.get('address')!r}")
        print(f"    dist={pd['dist_m']:.0f}m" if pd["dist_m"] is not None else "    dist=unknown",
              f" name_sim={pd['name_sim']:.2f}  sources={sorted(pd['sources'])}")
        print(f"    evidence: {pd['evidence']}")

    # ── Phase 4: parent_complex_id / is_umbrella sufficiency check ──────
    print("\n--- Phase 4: parent_complex_id / is_umbrella sufficiency ---")
    n_umbrella = sum(1 for c in complexes if c["is_umbrella"])
    n_with_parent = sum(1 for c in complexes if c["parent_complex_id"] is not None)
    sibling_pairs_without_parent_link = sum(
        1 for pd in pair_details if pd["relation"] in ("sibling_phase", "same_umbrella_project")
        and pd["a"].get("parent_complex_id") not in (pd["b"]["id"],) and pd["b"].get("parent_complex_id") not in (pd["a"]["id"],)
        and pd["a"].get("parent_complex_id") is None and pd["b"].get("parent_complex_id") is None
    )
    print(f"  is_umbrella=TRUE: {n_umbrella}   has parent_complex_id: {n_with_parent}")
    print(f"  sibling_phase/same_umbrella pairs found by THIS audit with NEITHER side already "
          f"linked via parent_complex_id: {sibling_pairs_without_parent_link}")
    print(f"  ('sibling'/'phase' pairs are peers, not parent/child — parent_complex_id models a "
          f"different relation (umbrella CONTAINS house); needs its own 'sibling group' concept, see report)")

    # ── Phase 6: recompute Tier A impact ────────────────────────────────
    print("\n--- Phase 6: recompute Tier A impact, sibling/dup-aware ---")
    # relation lookup per pair, for excluding conflict-listings whose
    # pair is genuinely ambiguous/sibling (must stay excluded) vs safe.
    relation_by_pair = {(pd["a"]["id"], pd["b"]["id"]): pd["relation"] for pd in pair_details}

    tier_a_recompute = []
    remaining_conflicts = 0
    resolved_as_duplicate = 0
    for l in listings:
        cids = sorted(set(exact_index.get(_norm_exact(l["complex_name"]), [])))
        if len(cids) != 1:
            continue
        existing_cid = existing_property_complex.get(l["id"])
        if existing_cid is not None and existing_cid != cids[0] and existing_cid in by_id:
            key = (min(existing_cid, cids[0]), max(existing_cid, cids[0]))
            rel = relation_by_pair.get(key, "unclassified")
            if rel == "duplicate_same_complex":
                resolved_as_duplicate += 1  # было бы safe ПОСЛЕ будущего merge, не сейчас
            remaining_conflicts += 1
            continue  # НИКАКОГО auto-write независимо от relation — задача явно
        tier_a_recompute.append(l["id"])

    print(f"  Tier A candidates unaffected by conflicts: {len(tier_a_recompute)}")
    print(f"  Conflicts total: {remaining_conflicts} "
          f"(of which classified as likely duplicate_same_complex, would resolve automatically "
          f"IF/WHEN those complex records get merged in a future decision: {resolved_as_duplicate})")
    print(f"  Conflicts classified as sibling_phase/same_umbrella (genuinely ambiguous, need "
          f"per-listing address disambiguation, NOT auto-resolvable by name alone): "
          f"{sum(1 for pd in pair_details if pd['relation'] in ('sibling_phase','same_umbrella_project') and pd['n_conflict_listings']>0)}")
    print(f"  Conflicts classified as separate_neighbor_complex (real resolver-risk cases): "
          f"{sum(1 for pd in pair_details if pd['relation']=='separate_neighbor_complex' and pd['n_conflict_listings']>0)}")

    print("\n" + "=" * 78)
    print("НИЧЕГО не записано в БД — этот скрипт read-only от начала до конца.")
    print("=" * 78)


if __name__ == "__main__":
    asyncio.run(main())
