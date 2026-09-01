#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/compute_complex_identity_review_impact.py — задача 2026-08-31,
"Complex Identity: human labeling + impact assessment", шаг 5. Read-only,
пишет ТОЛЬКО в локальный JSON-отчёт, ничего в БД.

Вход: JSON, экспортированный review-инструментом (Complex Identity —
разметка пар ЖК, "Экспортировать JSON"), тот же формат, что читает
scripts/import_reviewed_complex_relations.py. Считает impact ТОЛЬКО по
`relations_for_import` (реальные human labels, relation_type != null) —
`unreviewed_pairs` и `ambiguous_reviewed` НЕ участвуют в "resolved"
метриках (задача явно: "не подменять human label rule-based
классификатором" — если пара не размечена человеком, для целей impact
она остаётся conflict, конец истории, здесь не достраивается).

Переиспользует ТУ ЖЕ логику Tier A / conflict, что
scripts/audit_complex_sibling_duplicate_resolution.py Phase 6 (тот же
критерий "listing.complex_name резолвится РОВНО в один complex_id" и
"существующий properties.complex_id конфликтует с этим id") — не новая
эвристика, тот же прецедент, переиспользованный на подмножество пар,
для которых теперь есть human relation_type вместо unclassified.

Метрики (в точности п.5 задачи):
  1. сколько conflicts разрешено (reviewed, relation_type != ambiguous)
     vs остаётся (unreviewed + ambiguous_reviewed)
  2. breakdown duplicate/rename/sibling/umbrella/separate среди reviewed
  3. сколько Tier A listing resolutions стало deterministic — то есть
     ПЕРЕСТАЛИ быть "conflict" (relation резолвит confusion), но НЕ
     auto-resolved в resolved_house_id/complex_id прямо сейчас: distinct
     подсчёт "deterministic классификация конфликта" ("почему listing
     стоит на паузе" теперь известно) vs "auto-writable" (НЕТ — задача
     явно запрещает auto-write, эта таблица только считает потенциал)
  4. насколько вырос ПОТЕНЦИАЛЬНЫЙ resolved_house_id coverage — сколько
     listings с resolved_house_id IS NULL сидят на конфликте, который
     reviewed пара пометила duplicate/renamed (стало бы safe ПОСЛЕ
     будущего решения слить/канонизировать записи, не сейчас)
  5. сколько properties.complex_id можно БЕЗОПАСНО дополнить — то же
     самое, но properties, не listings (проекция, не запись)
  6. сколько ЖК перешли порог >=5 active properties — для duplicate/
     renamed reviewed-пар: если объединённый active-property count
     обеих сторон >= 5, а по отдельности хотя бы одна < 5 — считается
     ("active" = property имеет >=1 listing с is_active=TRUE, тот же
     прокси, что bot/core/complex_market_profile.py active_properties_now,
     упрощённый до "сейчас", без as_of — см. докстринг ниже)

Финальный verdict (A/B/C) печатается в конце, простое правило:
  reviewed_count == 0                             -> B (нечего считать)
  reviewed_count < 100 (top-100 не размечен целиком) -> B
  reviewed_count == 100 и >=80% решаемых пар НЕ ambiguous -> A
  reviewed_count == 100 и >=40% пар остались ambiguous     -> C
  иначе (100 размечено, но много ambiguous, не большинство)  -> B
Это НЕ ML-порог, а простое эвристическое read-out для человека —
финальное решение делает человек, не скрипт (та же граница
ответственности, что во всём остальном этом слое).

    venv/bin/python scripts/compute_complex_identity_review_impact.py <exported.json>
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

_RESOLVABLE = {"duplicate_same_complex", "renamed_same_complex"}


def _norm_exact(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip()).lower()


async def main() -> None:
    if len(sys.argv) < 2:
        print("usage: venv/bin/python scripts/compute_complex_identity_review_impact.py <exported.json>")
        sys.exit(1)
    with open(sys.argv[1], encoding="utf-8") as f:
        payload = json.load(f)

    from bot.db.pg import close_pool, fetch, init_pool
    await init_pool(DATABASE_URL)
    try:
        await run(payload, fetch)
    finally:
        await close_pool()


async def run(payload: dict, fetch) -> None:
    reviewed = [r for r in payload.get("relations_for_import", [])]
    ambiguous = payload.get("ambiguous_reviewed", [])
    unreviewed = payload.get("unreviewed_pairs", [])
    total_pairs = payload.get("total_pairs", len(reviewed) + len(ambiguous) + len(unreviewed))

    print("=" * 78)
    print("Complex Identity — review impact assessment")
    print("=" * 78)
    print(f"total top-100 pairs: {total_pairs}")
    print(f"reviewed (non-ambiguous label): {len(reviewed)}")
    print(f"reviewed as ambiguous: {len(ambiguous)}")
    print(f"unreviewed: {len(unreviewed)}")

    # ── 1/2: conflicts resolved + breakdown ─────────────────────────────
    by_relation = Counter(r["relation_type"] for r in reviewed)
    print("\n--- breakdown among reviewed (non-ambiguous) pairs ---")
    for rel in ("duplicate_same_complex", "renamed_same_complex", "sibling_phase",
                "same_umbrella_project", "separate_neighbor_complex"):
        print(f"  {rel:28s} {by_relation.get(rel, 0)}")
    n_resolvable = sum(by_relation.get(r, 0) for r in _RESOLVABLE)
    n_remaining = len(ambiguous) + len(unreviewed)
    print(f"\nconflicts classified as directly resolvable (duplicate/renamed): {n_resolvable}")
    print(f"conflicts classified but genuinely distinct (sibling/umbrella/separate — "
          f"still need per-listing disambiguation, not name-level auto-resolve): "
          f"{len(reviewed) - n_resolvable}")
    print(f"conflicts still unresolved (ambiguous + unreviewed): {n_remaining}")

    if not reviewed and not ambiguous:
        print("\nNo pairs reviewed yet — nothing further to compute against the DB.")
        _write_report(payload, {
            "reviewed_count": 0, "resolvable_count": 0, "by_relation": {},
            "tier_a_deterministic_listings": 0, "resolved_house_id_potential": 0,
            "complex_id_backfill_potential": 0, "complexes_crossing_5_active": [],
        })
        _print_verdict(total_pairs, len(reviewed), len(ambiguous), len(unreviewed))
        return

    resolvable_pairs = {(r["complex_id_a"], r["complex_id_b"]): r["relation_type"]
                         for r in reviewed if r["relation_type"] in _RESOLVABLE}

    # ── fresh read-only DB pull — same shape as audit_complex_sibling_duplicate_resolution.py ──
    complexes = await fetch("SELECT id, name FROM complexes WHERE COALESCE(is_garbage, FALSE) = FALSE")
    by_id = {c["id"]: c for c in complexes}
    exact_index: dict[str, list[int]] = {}
    for c in complexes:
        exact_index.setdefault(_norm_exact(c["name"]), []).append(c["id"])

    listings = await fetch(
        "SELECT al.id, al.complex_name, al.resolved_house_id, al.is_active "
        "FROM apartment_listings al WHERE al.complex_name IS NOT NULL AND al.complex_name <> ''"
    )
    prop_rows = await fetch(
        "SELECT pl.listing_id, pl.property_id, p.complex_id FROM property_listings pl "
        "JOIN properties p ON p.property_id = pl.property_id"
    )
    complex_of_property = {r["listing_id"]: r["complex_id"] for r in prop_rows}
    property_of_listing = {r["listing_id"]: r["property_id"] for r in prop_rows}

    # ── 3/4: Tier A deterministic + resolved_house_id potential ────────
    deterministic_listing_ids = set()
    resolved_house_id_potential_ids = set()
    for l in listings:
        cids = sorted(set(exact_index.get(_norm_exact(l["complex_name"]), [])))
        if len(cids) != 1:
            continue
        existing_cid = complex_of_property.get(l["id"])
        if existing_cid is None or existing_cid == cids[0] or existing_cid not in by_id:
            continue
        key = (min(existing_cid, cids[0]), max(existing_cid, cids[0]))
        if key in resolvable_pairs:
            deterministic_listing_ids.add(l["id"])
            if l["resolved_house_id"] is None:
                resolved_house_id_potential_ids.add(l["id"])

    print(f"\nTier A listing resolutions that became deterministic "
          f"(conflict now has a known human-confirmed cause, still NOT auto-written): "
          f"{len(deterministic_listing_ids)}")
    print(f"of which resolved_house_id IS NULL today (potential coverage growth "
          f"IF/WHEN a future merge decision is made — projection only, no write here): "
          f"{len(resolved_house_id_potential_ids)}")

    # ── 5: properties.complex_id safely backfillable (projection) ──────
    props_backfillable = set()
    prop_complex = {r["property_id"]: r["complex_id"] for r in prop_rows}
    for prop_id, cid in prop_complex.items():
        for (a_id, b_id), rel in resolvable_pairs.items():
            if cid in (a_id, b_id):
                props_backfillable.add(prop_id)
                break
    print(f"properties.complex_id rows that could be safely unified onto a canonical id "
          f"IF the corresponding duplicate/renamed pair is later merged (count, no write): "
          f"{len(props_backfillable)}")

    # ── 6: complexes crossing the >=5 active properties threshold ──────
    active_listing_ids = {l["id"] for l in listings if l["is_active"]}
    active_props_by_complex: dict[int, set] = {}
    for r in prop_rows:
        if r["listing_id"] in active_listing_ids:
            active_props_by_complex.setdefault(r["complex_id"], set()).add(r["property_id"])

    crossing = []
    for (a_id, b_id), rel in resolvable_pairs.items():
        a_active = len(active_props_by_complex.get(a_id, set()))
        b_active = len(active_props_by_complex.get(b_id, set()))
        combined = len(active_props_by_complex.get(a_id, set()) | active_props_by_complex.get(b_id, set()))
        if a_active < 5 and b_active < 5 and combined >= 5:
            crossing.append({"complex_id_a": a_id, "complex_id_b": b_id, "relation_type": rel,
                              "active_a": a_active, "active_b": b_active, "combined_active": combined})

    print(f"\nreviewed duplicate/renamed pairs where BOTH sides individually have <5 active "
          f"properties but the COMBINED count crosses >=5 (i.e. would newly qualify for whatever "
          f"downstream logic gates on that threshold, IF merged): {len(crossing)}")
    for c in crossing:
        print(f"  [{c['complex_id_a']} + {c['complex_id_b']}] {c['relation_type']} — "
              f"{c['active_a']} + {c['active_b']} -> combined {c['combined_active']}")

    metrics = {
        "reviewed_count": len(reviewed),
        "resolvable_count": n_resolvable,
        "by_relation": dict(by_relation),
        "tier_a_deterministic_listings": len(deterministic_listing_ids),
        "resolved_house_id_potential": len(resolved_house_id_potential_ids),
        "complex_id_backfill_potential": len(props_backfillable),
        "complexes_crossing_5_active": crossing,
    }
    _write_report(payload, metrics)
    _print_verdict(total_pairs, len(reviewed), len(ambiguous), len(unreviewed))


def _write_report(payload: dict, metrics: dict) -> None:
    out_path = os.path.join(os.path.dirname(__file__), "..", "complex_identity_review_impact_report.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "computed_at": datetime.now(timezone.utc).isoformat(),
            "source_export": payload.get("generated_from"),
            "source_exported_at": payload.get("exported_at"),
            "metrics": metrics,
        }, f, ensure_ascii=False, indent=2, default=str)
    print(f"\nreport written to {os.path.abspath(out_path)} (NOT committed — local artifact)")


def _print_verdict(total_pairs: int, n_reviewed: int, n_ambiguous: int, n_unreviewed: int) -> None:
    print("\n" + "=" * 78)
    if n_reviewed == 0:
        verdict, why = "B. MORE LABELING NEEDED", "0 pairs reviewed so far"
    elif n_reviewed + n_ambiguous < total_pairs:
        verdict, why = "B. MORE LABELING NEEDED", (
            f"only {n_reviewed + n_ambiguous}/{total_pairs} top-100 pairs reviewed "
            f"({n_unreviewed} still untouched)")
    else:
        ambiguous_share = n_ambiguous / total_pairs if total_pairs else 1.0
        if ambiguous_share >= 0.40:
            verdict, why = "C. COMPLEX DATA MODEL STILL INSUFFICIENT", (
                f"{ambiguous_share:.0%} of the fully-reviewed top-100 stayed ambiguous even "
                "after human review — the available fields likely can't disambiguate this "
                "slice, not a labeling-effort problem")
        elif ambiguous_share <= 0.20:
            verdict, why = "A. READY FOR SMALL TIER-A CANARY", (
                f"top-100 fully reviewed, only {ambiguous_share:.0%} ambiguous")
        else:
            verdict, why = "B. MORE LABELING NEEDED", (
                f"top-100 fully reviewed but {ambiguous_share:.0%} ambiguous — borderline, "
                "widen the reviewed set before a canary")
    print(f"VERDICT: {verdict}")
    print(f"  ({why})")
    print("=" * 78)
    print("\nЭтот verdict — read-out для человека, не разрешение на auto-link/merge/write. "
          "Никаких production writes от этого скрипта.")


if __name__ == "__main__":
    asyncio.run(main())
