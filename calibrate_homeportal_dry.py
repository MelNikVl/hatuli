#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Холостая (dry, ничего не пишет) калибровка score_match() на уже
сматченных homeportal_objects — "как будто видим кандидата впервые"
(тот же приём, что во всех предыдущих раундах калибровки 2026-08-12,
см. docs/entity_resolution_plan.md). Задача гейта 2, шаг 2: повторить
калибровку под самым свежим address_match() (зачистка "р."/"уч."/
район — коммит "address_match(): район/уч. — тоже шум, не сигнал") и
дать дрейф-отчёт относительно последней зафиксированной таблицы
(21 auto / 38 review / 1 skip).

ВАЖНО про выборку: предыдущие раунды использовали "те же 60 object_id"
между собой, но конкретный список id никогда не был сохранён в
репозиторий (только агрегаты в docs/entity_resolution_plan.md) — здесь
берём детерминированную выборку (ORDER BY object_id ASC LIMIT N среди
уже сматченных, geo уже прошло карантин placeholder-значений прямо в
колонках) для воспроизводимости этого и будущих прогонов. Сравнение с
21/38/1 — по популяции, не гарантированно побитово тот же набор id.

Запуск: venv/bin/python calibrate_homeportal_dry.py [--n 60]
"""
import argparse
import asyncio
import sys

sys.path.insert(0, ".")
from dotenv import load_dotenv
load_dotenv()
import os

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=60)
    args = ap.parse_args()

    from bot.db.pg import init_pool, close_pool, fetch, fetchrow, fetchval
    from bot.core.entity_resolution import score_match, AUTO_MATCH_THRESHOLD, REVIEW_QUEUE_THRESHOLD
    from hype_tracker.homeportal_scan import norm_name

    await init_pool(DATABASE_URL)

    rows = await fetch("""
        SELECT object_id, name, address, latitude, longitude, developer_bin, matched_complex_id
        FROM homeportal_objects
        WHERE matched_complex_id IS NOT NULL
        ORDER BY object_id ASC
        LIMIT $1
    """, args.n)
    print(f"выборка: {len(rows)} уже сматченных homeportal_objects (ORDER BY object_id ASC)")

    verdicts = {"auto": 0, "review": 0, "skip": 0}
    phase_hits = 0
    address_hits = 0
    details = []
    for r in rows:
        cx = await fetchrow("SELECT name, lat, lon, address FROM complexes WHERE id = $1", r["matched_complex_id"])
        if not cx:
            continue
        dev_bin = await fetchval("SELECT developer_bin FROM complex_tech_specs WHERE complex_id = $1", r["matched_complex_id"])
        conf, method = await score_match(
            norm_name(r["name"]), norm_name(cx["name"]),
            existing_lat=cx["lat"], existing_lon=cx["lon"],
            candidate_lat=float(r["latitude"]) if r["latitude"] else None,
            candidate_lon=float(r["longitude"]) if r["longitude"] else None,
            developer_match=bool(dev_bin) and dev_bin == r["developer_bin"],
            existing_address=cx["address"], candidate_address=r["address"],
            name_a_full=r["name"], name_b_full=cx["name"],
        )
        if conf >= AUTO_MATCH_THRESHOLD:
            v = "auto"
        elif conf >= REVIEW_QUEUE_THRESHOLD:
            v = "review"
        else:
            v = "skip"
        verdicts[v] += 1
        if "phase" in method:
            phase_hits += 1
        if "address" in method:
            address_hits += 1
        details.append((r["object_id"], r["name"], cx["name"], conf, method, v))

    for oid, na, nb, conf, method, v in details:
        print(f"  #{oid:5} {na[:35]!r:37} ~ {nb[:35]!r:37} conf={conf:.2f} {v:6} {method}")

    print(f"\nИТОГ ({len(details)} объектов): auto={verdicts['auto']} review={verdicts['review']} "
          f"skip={verdicts['skip']} | phase-сигнал сработал={phase_hits} | address-сигнал сработал={address_hits}")
    print("Сравнение с последней зафиксированной таблицей (docs/entity_resolution_plan.md, "
          f"иная выборка/60): было 21/38/1, сейчас {verdicts['auto']}/{verdicts['review']}/{verdicts['skip']}")

    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
