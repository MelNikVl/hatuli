#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/build_validation_manifest.py — задача 2026-08-18, follow-up
"Property Identity — calibration validation", п.2: FROZEN manifest из
200-300 случайных pending-пар со стратификацией, для БУДУЩЕЙ blind
ручной валидации, независимой от 152/158 уже принятых решений (Stage
1.1 этой же задачи явно помечены "exploratory calibration, НЕ
независимая проверка" — маленькая выборка, порядок очереди смещён,
рецензент видел фото).

## Что значит "frozen" здесь

Скрипт САМ НЕ пишет в property_match_candidates/property_match_
review_log — НИКАКИХ решений, НИКАКИХ merge, НИКАКИХ порогов auto-accept
(задача, явно, все три запрета). "Frozen" значит: снимок вычисленных
сигналов КАЖДОЙ отобранной пары фиксируется в JSON-файле на момент
генерации — если позже (например, после Stage 1.3 фото-канарейки или
после AI-стадии) те же кандидаты пересчитают evidence, manifest НЕ
меняется задним числом. Manifest — это ЗАФИКСИРОВАННЫЙ список
candidate_id + сигналы НА МОМЕНТ ОТБОРА, не live-запрос.

## Blind review — что это значит для БУДУЩЕГО UI (не реализован здесь)

Manifest хранит match_method/score/corroborating_methods/photo-сигналы —
это ФАКТЫ (сырые вычисленные признаки), не "рекомендация". Когда появится
реальный UI ручной валидации (отдельная задача — задача explicitly:
"Пока... подготовить только reproducible manifest и план подсчёта", без
UI), он должен показывать рецензенту ТОЛЬКО две карточки объявлений (как
/admin/property-match-review), БЕЗ поля "мы бы порекомендовали
accepted/rejected" — сигналы можно проверить ПОСЛЕ решения (audit trail),
не ДО (иначе это не blind validation, а confirmation bias).

## Стратификация

Ортогональные оси (НЕ cross-product — популяция слишком редкая в
некоторых сочетаниях, например semantic-сигнал СЕЙЧАС пуст на всей базе,
AI-стадия ни разу не запускалась в проде до этой задачи, кроме canary):

  match_method_bucket: exact_hash | dedup_listings | fuzzy_high(>=0.8) |
    fuzzy_medium(0.5-0.8)  — fuzzy_low(<0.5) СУЩЕСТВУЕТ как понятие в
    задаче, но популяция пуста (fuzzy score никогда < 0.5 на текущих
    данных, проверено прямым SELECT) — честно 0 в manifest, не подделано.
  photo_signal: exact | perceptual | semantic | no_match | no_evidence_yet
  corroborating_signals: 1 | 2 | 3+

Первичная стратификация — по match_method_bucket (самая населённая ось,
целевое количество per bucket пропорционально доступной популяции,
округлено к общему таргету 250). Внутри каждого bucket — детерминированная
случайная выборка (seed фиксирован явно, --seed, дефолт 20260818) через
`ORDER BY md5(candidate_id::text || seed)` — воспроизводимо: тот же seed
на тех же данных даёт тот же manifest.

## Запуск

    venv/bin/python scripts/build_validation_manifest.py --seed 20260818 --target 250
    venv/bin/python scripts/build_validation_manifest.py --seed 20260818 --target 250 --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

_BUCKETS = {
    "exact_hash": "pmc.match_method = 'exact_hash'",
    "dedup_listings": "pmc.match_method = 'dedup_listings'",
    "fuzzy_high": "pmc.match_method = 'fuzzy' AND pmc.match_score >= 0.8",
    "fuzzy_medium": "pmc.match_method = 'fuzzy' AND pmc.match_score >= 0.5 AND pmc.match_score < 0.8",
    "fuzzy_low": "pmc.match_method = 'fuzzy' AND pmc.match_score < 0.5",
}


async def _bucket_population(bucket_where: str) -> int:
    from bot.db.pg import fetchval
    return await fetchval(
        f"SELECT count(*) FROM property_match_candidates pmc WHERE pmc.status='pending' AND {bucket_where}")


_SELECT_COLS = """
    pmc.candidate_id, pmc.listing_id, pmc.candidate_property_id, pmc.match_method,
    pmc.match_score, pmc.relationship_type, pmc.evidence, pmc.conflict_reasons,
    pmc.matcher_version,
    pcpe.exact_shared_count, pcpe.perceptual_shared_count, pcpe.ai_similar_count,
    pcpe.processing_status AS photo_processing_status
"""


def _photo_signal(row: dict) -> str:
    if row.get("photo_processing_status") is None:
        return "no_evidence_yet"
    exact = row.get("exact_shared_count") or 0
    perceptual = row.get("perceptual_shared_count") or 0
    ai = row.get("ai_similar_count") or 0
    if exact > 0:
        return "exact"
    if perceptual > 0:
        return "perceptual"
    if ai > 0:
        return "semantic"
    return "no_match"


def _n_corroborating(row: dict) -> int:
    ev = row.get("evidence")
    if isinstance(ev, str):
        ev = json.loads(ev)
    methods = (ev or {}).get("corroborating_methods") or [row["match_method"]]
    return len(methods)


async def _sample_where(where: str, n: int, seed: int, exclude: set[int]) -> list[dict]:
    from bot.db.pg import fetch
    if n <= 0:
        return []
    params = [str(seed), n]
    exclude_clause = ""
    if exclude:
        exclude_clause = "AND pmc.candidate_id != ALL($3::int[])"
        params.append(list(exclude))
    rows = await fetch(f"""
        SELECT {_SELECT_COLS}
        FROM property_match_candidates pmc
        LEFT JOIN property_candidate_photo_evidence pcpe ON pcpe.candidate_id = pmc.candidate_id
        WHERE pmc.status = 'pending' AND ({where}) {exclude_clause}
        ORDER BY md5(pmc.candidate_id::text || $1::text)
        LIMIT $2
    """, *params)
    return [dict(r) for r in rows]


# Гарантированные страты (задача, явно перечисляет photo exact/perceptual/
# semantic/no_match и 1/2/3+ corroborating как ОБЯЗАТЕЛЬНЫЕ оси, не только
# match_method) — ФАЗА 1 отбирает их НАПЕРВЫЙ, малой квотой каждая, чтобы
# редкие сочетания (corroboration>=2 — всего ~70-100 на всю базу,
# photo exact/perceptual — только среди уже посчитанных Stage 1.3 canary)
# не потерялись за пропорциональной выборкой по match_method (ФАЗА 2).
_GUARANTEED_STRATA = [
    ("corroboration_2plus", "jsonb_array_length(COALESCE(pmc.evidence->'corroborating_methods', '[]'::jsonb)) >= 2", 30),
    ("photo_exact", "EXISTS (SELECT 1 FROM property_candidate_photo_evidence x WHERE x.candidate_id=pmc.candidate_id AND x.exact_shared_count > 0)", 20),
    ("photo_perceptual", "EXISTS (SELECT 1 FROM property_candidate_photo_evidence x WHERE x.candidate_id=pmc.candidate_id AND x.perceptual_shared_count > 0 AND x.exact_shared_count = 0)", 20),
    ("photo_semantic", "EXISTS (SELECT 1 FROM property_candidate_photo_evidence x WHERE x.candidate_id=pmc.candidate_id AND x.ai_similar_count > 0)", 10),
    ("photo_no_match_computed", "EXISTS (SELECT 1 FROM property_candidate_photo_evidence x WHERE x.candidate_id=pmc.candidate_id AND x.processing_status='ok' AND x.exact_shared_count=0 AND x.perceptual_shared_count=0 AND x.ai_similar_count=0)", 15),
]


async def build_manifest(seed: int, target: int) -> dict:
    manifest_rows: list[dict] = []
    picked_ids: set[int] = set()
    guaranteed_achieved = {}

    # ── ФАЗА 1: гарантированное покрытие редких страт ────────────────────
    for name, where, quota in _GUARANTEED_STRATA:
        rows = await _sample_where(where, quota, seed, exclude=picked_ids)
        guaranteed_achieved[name] = {"requested": quota, "achieved": len(rows)}
        for r in rows:
            r["stratum_guaranteed"] = name
            r["stratum_photo_signal"] = _photo_signal(r)
            r["stratum_n_corroborating"] = _n_corroborating(r)
            manifest_rows.append(r)
            picked_ids.add(r["candidate_id"])

    print(f"ФАЗА 1 (гарантированные страты): {guaranteed_achieved}")

    # ── ФАЗА 2: пропорциональное добавление по match_method_bucket до target ──
    populations = {b: await _bucket_population(w) for b, w in _BUCKETS.items()}
    total_population = sum(populations.values())
    print(f"Доступная популяция по match_method-бакетам: {populations} (всего: {total_population})")

    remaining_target = max(target - len(manifest_rows), 0)
    allocation = {}
    nonzero_buckets = [b for b, n in populations.items() if n > 0]
    for b in nonzero_buckets:
        share = round(remaining_target * populations[b] / total_population) if total_population else 0
        allocation[b] = min(share, populations[b])
    diff = remaining_target - sum(allocation.values())
    if diff != 0 and nonzero_buckets:
        biggest = max(nonzero_buckets, key=lambda b: populations[b])
        allocation[biggest] = max(0, allocation[biggest] + diff)

    print(f"ФАЗА 2 целевое распределение (remaining={remaining_target}): {allocation}")

    for bucket, where in _BUCKETS.items():
        n = allocation.get(bucket, 0)
        rows = await _sample_where(where, n, seed, exclude=picked_ids)
        for r in rows:
            r["stratum_match_method_bucket"] = bucket
            r["stratum_guaranteed"] = None
            r["stratum_photo_signal"] = _photo_signal(r)
            r["stratum_n_corroborating"] = _n_corroborating(r)
            manifest_rows.append(r)
            picked_ids.add(r["candidate_id"])

    # Кросс-табуляция достигнутого покрытия (задача просит показать
    # РЕАЛЬНОЕ покрытие по фото-сигналу и corroboration, честно —
    # НЕ форсированное равным).
    photo_signal_counts = Counter(r["stratum_photo_signal"] for r in manifest_rows)
    corrob_counts = Counter(
        "3+" if r["stratum_n_corroborating"] >= 3 else str(r["stratum_n_corroborating"])
        for r in manifest_rows
    )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "seed": seed, "target": target,
        "guaranteed_strata_achieved": guaranteed_achieved,
        "bucket_populations": populations,
        "bucket_allocation": allocation,
        "actual_count": len(manifest_rows),
        "photo_signal_coverage_achieved": dict(photo_signal_counts),
        "corroboration_coverage_achieved": dict(corrob_counts),
        "rows": manifest_rows,
    }


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, default=20260818)
    ap.add_argument("--target", type=int, default=250)
    ap.add_argument("--out", type=str, default="validation_manifest.json")
    ap.add_argument("--dry-run", action="store_true", help="не писать файл, только печать сводки")
    args = ap.parse_args()

    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    try:
        manifest = await build_manifest(args.seed, args.target)
    finally:
        await close_pool()

    summary = {k: v for k, v in manifest.items() if k != "rows"}
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))

    if not args.dry_run:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2, default=str)
        print(f"\nMANIFEST записан в {args.out} ({manifest['actual_count']} пар). "
              f"Никакие решения/статусы не изменены — только SELECT + локальный файл.")


if __name__ == "__main__":
    asyncio.run(main())
