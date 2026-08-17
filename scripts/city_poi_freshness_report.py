#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""city_poi_freshness_report.py — отчёт/alert по свежести city_poi
категорий (задача 2026-08-17, "City POI timer" — п.3: "count,
last_updated, freshness_days, stale >14/30 дней"). Read-only, ничего не
пишет.

Пороги 14/30 дней — те же, что уже использует bot/core/location_score.py
::_apply_freshness_confidence_penalty (0.8x confidence >14д, 0.5x >30д)
— один и тот же язык "устарело" по всему проекту, не два разных порога
для одной и той же категории.

Запуск:
    venv/bin/python scripts/city_poi_freshness_report.py
    venv/bin/python scripts/city_poi_freshness_report.py --json
    venv/bin/python scripts/city_poi_freshness_report.py --alert-only
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

STALE_WARN_DAYS = 14.0
STALE_CRIT_DAYS = 30.0

# Все kind, которые sync_city_poi.py умеет заполнять (см. его CATEGORIES) —
# включены в отчёт, даже если ЕЩЁ ни разу не синхронизированы (count=0,
# last_updated=None, freshness_days=None — статус "never_synced", не
# "stale": другое дело, см. bot/score_layers/osm.py::kinds_synced).
_ALL_SYNC_KINDS = [
    "school", "kindergarten", "university", "bus_stop", "hospital", "clinic",
    "pharmacy", "shop", "mall", "food", "park", "sports", "industrial",
    "cemetery", "landfill", "road_major", "road_secondary", "railway", "bar",
]


def _status(freshness_days: float | None) -> str:
    if freshness_days is None:
        return "never_synced"
    if freshness_days > STALE_CRIT_DAYS:
        return "stale_30"
    if freshness_days > STALE_WARN_DAYS:
        return "stale_14"
    return "fresh"


async def build_report() -> list[dict]:
    from bot.db.pg import fetch
    rows = await fetch(
        "SELECT kind, count(*) AS n, max(updated_at) AS newest, min(updated_at) AS oldest "
        "FROM city_poi WHERE kind = ANY($1::text[]) GROUP BY kind",
        _ALL_SYNC_KINDS,
    )
    by_kind = {r["kind"]: r for r in rows}
    now = datetime.now(timezone.utc)
    out = []
    for kind in _ALL_SYNC_KINDS:
        r = by_kind.get(kind)
        if r is None or r["n"] == 0:
            out.append({"kind": kind, "count": 0, "last_updated": None,
                        "freshness_days": None, "status": "never_synced"})
            continue
        # freshness = возраст САМОЙ СТАРОЙ строки этой kind (тот же принцип,
        # что bot/score_layers/osm.py::city_poi_freshness_days — "самое
        # слабое звено", не средняя/самая свежая запись) — но т.к. save_
        # category() пишет kind ОДНОЙ атомарной транзакцией (DELETE+INSERT),
        # oldest==newest на практике всегда (весь kind обновляется разом);
        # используем oldest на случай будущих частичных обновлений.
        age_days = (now - r["oldest"]).total_seconds() / 86400.0
        out.append({
            "kind": kind, "count": r["n"],
            "last_updated": r["newest"].isoformat(),
            "freshness_days": round(age_days, 1),
            "status": _status(age_days),
        })
    return out


def _print_table(report: list[dict]) -> None:
    print(f"{'kind':<16}{'count':>8}{'freshness_days':>16}{'status':>14}  last_updated")
    for r in report:
        fd = f"{r['freshness_days']:.1f}" if r["freshness_days"] is not None else "-"
        lu = r["last_updated"] or "-"
        print(f"{r['kind']:<16}{r['count']:>8}{fd:>16}{r['status']:>14}  {lu}")


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="вывод JSON вместо таблицы")
    ap.add_argument("--alert-only", action="store_true",
                     help="показать только stale_14/stale_30/never_synced (для cron-алерта)")
    args = ap.parse_args()

    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    try:
        report = await build_report()
    finally:
        await close_pool()

    if args.alert_only:
        report = [r for r in report if r["status"] != "fresh"]

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_table(report)

    n_stale = sum(1 for r in report if r["status"] in ("stale_14", "stale_30"))
    n_never = sum(1 for r in report if r["status"] == "never_synced")
    if not args.json:
        print(f"\nstale (>14д): {n_stale}, never_synced: {n_never}")
    # exit code для крона/systemd — ненулевой при stale_30 или never_synced
    # на категории, у которых ЕСТЬ потребитель (см. модульный докстринг
    # sync_city_poi.py — roads/railway/nightlife "данные про запас,
    # потребителя пока нет" НЕ считаем критичными для кода выхода, только
    # для видимости в отчёте).
    _has_consumer = {"school", "kindergarten", "university", "bus_stop",
                      "hospital", "clinic", "pharmacy", "shop", "mall",
                      "food", "park", "road_major", "road_secondary"}
    critical = [r for r in report if r["kind"] in _has_consumer
                and r["status"] in ("stale_30", "never_synced")]
    if critical:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
