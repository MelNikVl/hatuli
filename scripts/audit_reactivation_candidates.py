#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/audit_reactivation_candidates.py — задача 2026-08-17, follow-up
на fix(archive-check): read-only аудит строк, которые bot.core.archive_
check.reactivate_reappeared_listings() реактивирует (is_active=FALSE,
archived_at IS NOT NULL, last_seen > archived_at).

READ-ONLY. Ничего не пишет и не реактивирует — задача, явно: "Ничего из
этих 211 пока не обновляй". Единственная цель — проверить ПЕРЕД деплоем,
что "last_seen ушёл вперёд archived_at" на самом деле означает "объявление
снова появилось на Крыше", а не какой-то другой артефакт (напр. ошибочно
подвинутый last_seen без реального re-parse'а).

## Что показывает

1. Распределение по датам — сколько строк на какую "давность" архивации
   (archived_at) и какой разрыв (last_seen - archived_at, в днях).
2. Ручная проверка (РЕАЛЬНЫЙ HTTP GET, тот же bot.core.archive_check.
   _check_one, что использует сама архивация — не выдуманная логика) —
   20 примеров, распределённых РАВНОМЕРНО по отсортированному списку (не
   только первые 20 — чтобы не задеть только один конец распределения),
   результат: 'alive' (подтверждает реактивацию), 'archived'/'deleted'
   (last_seen мог быть ложно подвинут — ставит гипотезу под вопрос),
   None (сеть/блокировка, не подтверждает и не опровергает).

Запуск: venv/bin/python scripts/audit_reactivation_candidates.py [--sample N] [--json]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")


async def _fetch_candidates() -> list[dict]:
    from bot.db.pg import fetch
    rows = await fetch("""
        SELECT id, url, archived_at, archive_reason, last_seen,
               EXTRACT(EPOCH FROM (last_seen - archived_at)) / 86400.0 AS gap_days
        FROM apartment_listings
        WHERE is_active = FALSE AND archived_at IS NOT NULL
          AND last_seen IS NOT NULL AND last_seen > archived_at
        ORDER BY id
    """)
    return [dict(r) for r in rows]


def _date_distribution(rows: list[dict]) -> dict:
    from collections import Counter
    by_archived_week: Counter = Counter()
    gap_buckets: Counter = Counter()
    for r in rows:
        week = r["archived_at"].strftime("%Y-W%V")
        by_archived_week[week] += 1
        gap = float(r["gap_days"])
        if gap < 1:
            bucket = "<1 день"
        elif gap < 3:
            bucket = "1-3 дня"
        elif gap < 7:
            bucket = "3-7 дней"
        elif gap < 14:
            bucket = "7-14 дней"
        elif gap < 30:
            bucket = "14-30 дней"
        else:
            bucket = "30+ дней"
        gap_buckets[bucket] += 1
    return {
        "by_archived_week": dict(sorted(by_archived_week.items())),
        "by_gap_bucket": dict(gap_buckets),
    }


def _evenly_spaced_sample(rows: list[dict], n: int) -> list[dict]:
    """n примеров, РАВНОМЕРНО распределённых по отсортированному списку
    (не только первые N) — задача, явно: не однобокая выборка."""
    if len(rows) <= n:
        return rows
    step = len(rows) / n
    return [rows[int(i * step)] for i in range(n)]


async def _manual_verify(rows: list[dict]) -> list[dict]:
    """РЕАЛЬНЫЙ HTTP GET — та же bot.core.archive_check._check_one, что
    использует сама архивация/реактивация (не вторая копия логики)."""
    import httpx
    from bot.core.archive_check import _check_one, HEADERS

    results = []
    async with httpx.AsyncClient(headers=HEADERS, timeout=25.0, follow_redirects=True) as client:
        for r in rows:
            try:
                status = await _check_one(client, r["url"])
            except RuntimeError:
                status = "blocked"
            except Exception as exc:  # noqa: BLE001
                status = f"error: {exc}"
            results.append({
                "id": r["id"], "url": r["url"], "gap_days": round(float(r["gap_days"]), 1),
                "archived_at": r["archived_at"].isoformat(), "last_seen": r["last_seen"].isoformat(),
                "live_check_result": status,
            })
            await asyncio.sleep(2.5)
    return results


async def run_audit(sample_size: int) -> dict:
    rows = await _fetch_candidates()
    distribution = _date_distribution(rows)
    sample = _evenly_spaced_sample(rows, sample_size)
    verified = await _manual_verify(sample)

    confirms_reappeared = sum(1 for v in verified if v["live_check_result"] == "alive")
    contradicts = sum(1 for v in verified if v["live_check_result"] in ("archived", "deleted"))
    inconclusive = len(verified) - confirms_reappeared - contradicts

    return {
        "total_candidates": len(rows),
        "date_distribution": distribution,
        "sample_size": len(verified),
        "sample_confirms_reappeared_alive": confirms_reappeared,
        "sample_contradicts_still_archived_or_gone": contradicts,
        "sample_inconclusive": inconclusive,
        "sample_detail": verified,
    }


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sample", type=int, default=20)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    try:
        report = await run_audit(args.sample)
    finally:
        await close_pool()

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return

    print(f"\n=== reactivation candidates audit (READ-ONLY, ничего не изменено) ===\n")
    print(f"всего кандидатов на реактивацию: {report['total_candidates']}")
    print(f"\nпо дате архивации (неделя):")
    for week, cnt in report["date_distribution"]["by_archived_week"].items():
        print(f"  {week}: {cnt}")
    print(f"\nпо разрыву last_seen - archived_at:")
    for bucket, cnt in report["date_distribution"]["by_gap_bucket"].items():
        print(f"  {bucket}: {cnt}")
    print(f"\n--- ручная проверка {report['sample_size']} примеров (реальный HTTP GET) ---")
    for v in report["sample_detail"]:
        print(v)
    print(f"\nподтвердили реактивацию (alive сейчас): {report['sample_confirms_reappeared_alive']}/{report['sample_size']}")
    print(f"противоречат (всё ещё archived/deleted): {report['sample_contradicts_still_archived_or_gone']}/{report['sample_size']}")
    print(f"неопределённо (сеть/блок): {report['sample_inconclusive']}/{report['sample_size']}")


if __name__ == "__main__":
    asyncio.run(main())
