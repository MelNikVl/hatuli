#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/audit_photo_evidence_priority_canary.py — задача 2026-08-18,
"Property Identity — review calibration", Stage 1.3: расширенный canary
photo evidence на 1000 приоритетных pending-пар.

НЕ переписывает фотосопоставление (задача, явно: "его не переписывать") —
вся сравнивающая логика (sha256/phash/SigLIP/агрегация/пороги) остаётся в
bot/identity/photo_evidence.py::aggregate_candidate_evidence, буквально
та же функция, что вызывает scripts/photo_evidence_scan.py. Этот файл
добавляет ТОЛЬКО то, чего у существующего CLI нет:
  1) 4-уровневый приоритет отбора кандидатов (задача, явно, разный от
     --order strongest/id существующего скрипта: "1. два-три независимых
     сигнала, 2. exact-hash, 3. dedup_listings, 4. fuzzy с высоким score");
  2) live-гейт "STOP если error rate > 5% / появляются массовые ложные
     совпадения" (задача, явно) — существующий CLI молча доканчивает
     батч, здесь прерываемся РАНЬШЕ полного набора, если гейт сработал;
  3) сравнение с ручными решениями и оценку стоимости/времени полного
     скана оставшейся очереди.

## Причина отдельного файла, а не флага в scripts/photo_evidence_scan.py

scripts/photo_evidence_scan.py уже поддерживает --status/--order/--limit/
--min-score — ни один из них не выражает "top-1000 по 4 явным тиграм
метода/корроборации" без явного списка candidate_id. Добавлять сюда
пятый способ приоритезации ради одноразового calibration-audit усложнил
бы постоянный CLI ради временного запроса — отдельный audit-скрипт (тот
же паттерн, что scripts/audit_*.py в этом репо — временные read-mostly
инструменты, не части регулярного пайплайна) чище.

## Запуск (main venv, ФАЗА 1 — sha256/phash, без torch)

    venv/bin/python scripts/audit_photo_evidence_priority_canary.py --limit 1000
    venv/bin/python scripts/audit_photo_evidence_priority_canary.py --limit 1000 --dry-run

ФАЗА 2 (AI/SigLIP) — отдельно, другой venv, existing script НЕ трогаем:
    /home/nik/floorplan-clip/venv/bin/python scripts/photo_evidence_ai_scan.py --limit <N>

Затем повторный запуск ЭТОГО скрипта с --only-missing (или просто снова
без флага — aggregate_candidate_evidence идемпотентен, повторный вызов на
уже закэшированные фото сети не делает, только пересчитывает агрегацию
и подхватывает новые embedding/photo_type) финализирует processing_status
'partial' -> 'ok' для тех же 1000 кандидатов.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.FileHandler("photo_evidence_priority_canary.log", encoding="utf-8", errors="replace"),
              logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("photo_evidence_priority_canary")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

# Гейт задачи, явно: "Если error rate выше 5% ... — STOP и отчёт, полный
# прогон не запускать." MIN_SAMPLE_FOR_GATE — не судим по первым 2-3
# упавшим кандидатам (шумно), но и не ждём весь набор — проверяем гейт
# после каждого шага, начиная с этого минимума.
ERROR_RATE_STOP_THRESHOLD = 0.05
MIN_SAMPLE_FOR_GATE = 50


# 4-уровневый приоритет (задача, явно, п.1.3):
#   tier 0: >=2 corroborating methods (независимо от match_method) —
#           "два-три независимых сигнала";
#   tier 1: match_method='exact_hash' (single-signal);
#   tier 2: match_method='dedup_listings' (single-signal);
#   tier 3: match_method='fuzzy', ORDER BY match_score DESC ("высокий score" —
#           сортировка, не отдельный порог: задача не называет число).
_PRIORITY_SQL = """
    SELECT candidate_id, listing_id, candidate_property_id, match_method, match_score
    FROM property_match_candidates
    WHERE status = 'pending'
    ORDER BY
        CASE WHEN jsonb_array_length(COALESCE(evidence->'corroborating_methods', '[]'::jsonb)) >= 2 THEN 0 ELSE 1 END,
        CASE match_method WHEN 'exact_hash' THEN 0 WHEN 'dedup_listings' THEN 1 WHEN 'fuzzy' THEN 2 ELSE 3 END,
        match_score DESC,
        candidate_id ASC
    LIMIT $1
"""


async def _select_priority_candidates(limit: int, only_missing: bool) -> list[dict]:
    from bot.db.pg import fetch

    if only_missing:
        sql = _PRIORITY_SQL.replace(
            "WHERE status = 'pending'",
            "WHERE status = 'pending' AND NOT EXISTS (SELECT 1 FROM property_candidate_photo_evidence pcpe "
            "WHERE pcpe.candidate_id = property_match_candidates.candidate_id AND pcpe.processing_status = 'ok')",
        )
    else:
        sql = _PRIORITY_SQL
    rows = await fetch(sql, limit)
    return [dict(r) for r in rows]


async def run(limit: int, dry_run: bool, delay: float, only_missing: bool, batch_size: int) -> dict:
    import httpx

    from bot.identity.photo_evidence import aggregate_candidate_evidence

    candidates = await _select_priority_candidates(limit, only_missing)
    log.info("отобрано %d приоритетных pending-пар (only_missing=%s)%s",
              len(candidates), only_missing, " [DRY-RUN]" if dry_run else "")

    stats = {
        "requested": limit, "selected": len(candidates), "processed": 0, "errors": 0,
        "exact": 0, "perceptual": 0, "semantic": 0, "no_match": 0, "partial_no_ai_yet": 0,
    }
    stopped_early = False
    stop_reason = None
    per_candidate: list[dict] = []
    t0 = time.monotonic()

    async with httpx.AsyncClient(timeout=20.0) as client:
        for i, c in enumerate(candidates):
            try:
                evidence = await aggregate_candidate_evidence(
                    c["candidate_id"], http_client=client, delay=delay, dry_run=dry_run)
                error = None
            except Exception as exc:  # noqa: BLE001 — один упавший кандидат не должен ронять весь canary
                log.warning("candidate_id=%s упал: %s: %s", c["candidate_id"], type(exc).__name__, exc)
                evidence = {"processing_status": "error"}
                error = f"{type(exc).__name__}: {exc}"

            stats["processed"] += 1
            exact = evidence.get("exact_shared_count", 0) or 0
            perceptual = evidence.get("perceptual_shared_count", 0) or 0
            ai = evidence.get("ai_similar_count", 0) or 0
            pstatus = evidence.get("processing_status", "error")

            if pstatus == "error":
                stats["errors"] += 1
                category = "error"
            elif exact > 0:
                stats["exact"] += 1
                category = "exact"
            elif perceptual > 0:
                stats["perceptual"] += 1
                category = "perceptual"
            elif ai > 0:
                stats["semantic"] += 1
                category = "semantic"
            elif pstatus == "partial":
                # sha256/phash сделаны, AI-стадия ещё не отработала —
                # ФАЗА 2 может ещё поднять semantic, отдельная категория,
                # НЕ смешиваем с честным "нет совпадений вообще".
                stats["partial_no_ai_yet"] += 1
                category = "partial_no_ai_yet"
            else:
                stats["no_match"] += 1
                category = "no_match"

            per_candidate.append({
                "candidate_id": c["candidate_id"], "match_method": c["match_method"],
                "match_score": float(c["match_score"]) if c["match_score"] is not None else None,
                "category": category, "exact": exact, "perceptual": perceptual, "ai": ai,
                "processing_status": pstatus, "error": error,
            })

            if (i + 1) % max(batch_size, 1) == 0 or (i + 1) == len(candidates):
                elapsed = round(time.monotonic() - t0, 1)
                log.info("прогресс: %d/%d за %.1fс — %s", i + 1, len(candidates), elapsed,
                          {k: v for k, v in stats.items() if k not in ("requested", "selected")})

            # ── STOP-гейт (задача, явно) ──────────────────────────────
            if stats["processed"] >= MIN_SAMPLE_FOR_GATE:
                error_rate = stats["errors"] / stats["processed"]
                if error_rate > ERROR_RATE_STOP_THRESHOLD:
                    stopped_early = True
                    stop_reason = (f"error_rate={error_rate:.1%} > {ERROR_RATE_STOP_THRESHOLD:.0%} "
                                    f"после {stats['processed']} кандидатов — STOP по гейту задачи")
                    log.error(stop_reason)
                    break

    elapsed_total = round(time.monotonic() - t0, 1)
    stats["elapsed_sec"] = elapsed_total
    stats["stopped_early"] = stopped_early
    stats["stop_reason"] = stop_reason
    stats["per_candidate"] = per_candidate
    return stats


async def _remaining_queue_estimate(processed: int, elapsed_sec: float) -> dict:
    """Оценка времени/стоимости полного скана ОСТАВШЕЙСЯ очереди —
    задача, явно, п.1.3: "вывести ожидаемое время и стоимость полного
    сканирования оставшейся очереди". Экстраполяция ЛИНЕЙНАЯ по времени
    на кандидата этого canary — грубо (реальный кэш-хитрейт будет ниже
    на случайной остальной очереди, где меньше photo-переиспользования
    между соседними парами, чем в приоритетной top-1000), помечено явно."""
    from bot.db.pg import fetchval

    remaining_pending = await fetchval(
        "SELECT count(*) FROM property_match_candidates pmc WHERE pmc.status = 'pending' "
        "AND NOT EXISTS (SELECT 1 FROM property_candidate_photo_evidence pcpe "
        "WHERE pcpe.candidate_id = pmc.candidate_id AND pcpe.processing_status = 'ok')"
    )
    per_candidate_sec = (elapsed_sec / processed) if processed else None
    return {
        "remaining_pending_without_ok_evidence": remaining_pending,
        "canary_sec_per_candidate": round(per_candidate_sec, 3) if per_candidate_sec else None,
        "extrapolated_full_scan_hours": (
            round(remaining_pending * per_candidate_sec / 3600, 1) if per_candidate_sec else None
        ),
        "caveat": ("ЛИНЕЙНАЯ экстраполяция по среднему сек/кандидат ЭТОГО canary (приоритетные пары чаще делят "
                   "фото между собой -> выше cache-hit rate, чем у случайной остальной очереди) — верхняя "
                   "граница оптимистична, реальное время полного скана, вероятнее, БОЛЬШЕ."),
    }


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=1000)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--only-missing", action="store_true")
    ap.add_argument("--batch-size", type=int, default=25)
    ap.add_argument("--out", type=str, default="photo_evidence_priority_canary_result.json")
    args = ap.parse_args()

    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    try:
        stats = await run(args.limit, args.dry_run, args.delay, args.only_missing, args.batch_size)
        estimate = await _remaining_queue_estimate(stats["processed"], stats["elapsed_sec"])
    finally:
        await close_pool()

    summary = {k: v for k, v in stats.items() if k != "per_candidate"}
    summary["remaining_queue_estimate"] = estimate
    log.info("ИТОГ: %s", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    print(f"\nПолный результат (включая per_candidate) записан в {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
