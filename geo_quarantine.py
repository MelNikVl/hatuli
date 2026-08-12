#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Карантин битых координат (задача 2026-08-12, docs/entity_resolution_plan.md
— аудит blob-комплексов нашёл Dream City (homeportal-точка в 899 км от
остальных — Туркестан вместо Астаны) и GreenLine. Headliner Exclusive
(474 км — Караганда). Ни один парсер не валидировал гео на входе.

Метод: для каждого ЖК с 2+ гео-точками (complexes.lat/lon + все
привязанные homeportal_objects.lat/lon) считаем медиану lat и медиану lon
отдельно (устойчива к выбросам, пока их меньшинство). Любая точка дальше
50 км от медианы — в карантин: обнуляется НА УРОВНЕ ЗНАЧЕНИЯ (не всей
строки), исходное значение сохраняется в geo_quarantined_lat/lon на
случай отката. Если после карантина у complexes.lat/lon не осталось
доверенного значения — пересчитываем: медиана оставшихся хороших точек,
либо re-geocode по адресу (bot.core.geo.geocode).

Идемпотентен — уже закарантиненные точки (geo_quarantined_at IS NOT
NULL) не участвуют в наборе точек повторно.

Запуск: venv/bin/python geo_quarantine.py [--test]
"""
import argparse
import asyncio
import statistics
import sys

sys.path.insert(0, ".")
from dotenv import load_dotenv
load_dotenv()
import os

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")
QUARANTINE_RADIUS_M = 50_000.0


def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    import math
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="только посчитать и напечатать, без записи")
    args = ap.parse_args()

    from bot.db.pg import init_pool, close_pool, fetch, fetchrow, execute
    await init_pool(DATABASE_URL)

    complexes = await fetch("SELECT id, name, lat, lon, address FROM complexes WHERE geo_quarantined_at IS NULL")
    hp_all = await fetch("""
        SELECT object_id, matched_complex_id, latitude, longitude
        FROM homeportal_objects
        WHERE matched_complex_id IS NOT NULL AND geo_quarantined_at IS NULL
          AND latitude IS NOT NULL AND longitude IS NOT NULL
          AND latitude ~ '^[0-9.]+$' AND longitude ~ '^[0-9.]+$'
    """)
    hp_by_complex: dict[int, list] = {}
    for r in hp_all:
        hp_by_complex.setdefault(r["matched_complex_id"], []).append(r)

    quarantined_hp = 0
    quarantined_cx = 0
    recomputed = 0
    reports = []

    for c in complexes:
        cid = c["id"]
        hp_points = hp_by_complex.get(cid, [])
        points = []  # (kind, ref, lat, lon)
        if c["lat"] is not None and c["lon"] is not None:
            points.append(("complex", None, c["lat"], c["lon"]))
        for h in hp_points:
            points.append(("homeportal", h["object_id"], float(h["latitude"]), float(h["longitude"])))
        if len(points) < 2:
            continue

        median_lat = statistics.median(p[2] for p in points)
        median_lon = statistics.median(p[3] for p in points)

        bad = []
        for kind, ref, lat, lon in points:
            dist = _haversine_m(median_lat, median_lon, lat, lon)
            if dist > QUARANTINE_RADIUS_M:
                bad.append((kind, ref, lat, lon, dist))
        if not bad:
            continue

        report = {"complex_id": cid, "name": c["name"], "before_complex_lat": c["lat"], "before_complex_lon": c["lon"],
                   "bad": bad, "after_complex_lat": c["lat"], "after_complex_lon": c["lon"]}

        for kind, ref, lat, lon, dist in bad:
            reason = f"дальше {dist/1000:.0f} км от медианы ЖК ({median_lat:.4f},{median_lon:.4f}), карантин 2026-08-12"
            if kind == "homeportal":
                quarantined_hp += 1
                if not args.test:
                    await execute("""
                        UPDATE homeportal_objects SET
                            geo_quarantined_lat = latitude, geo_quarantined_lon = longitude,
                            latitude = NULL, longitude = NULL,
                            geo_quarantined_at = now(), geo_quarantine_reason = $2
                        WHERE object_id = $1
                    """, ref, reason)
            else:  # complex
                quarantined_cx += 1
                if not args.test:
                    await execute("""
                        UPDATE complexes SET
                            geo_quarantined_lat = lat, geo_quarantined_lon = lon,
                            lat = NULL, lon = NULL,
                            geo_quarantined_at = now(), geo_quarantine_reason = $2
                        WHERE id = $1
                    """, cid, reason)
                report["after_complex_lat"] = None
                report["after_complex_lon"] = None

        # Пересчёт complexes.lat/lon, если своей точки не осталось (была
        # закарантинена сейчас ИЛИ была NULL с самого начала) — медиана
        # выживших хороших точек (homeportal, не попавших в bad), иначе
        # re-geocode по адресу.
        if report["after_complex_lat"] is None:
            bad_refs = {(kind, ref) for kind, ref, *_ in bad}
            good_points = [(lat, lon) for kind, ref, lat, lon in points
                           if kind == "homeportal" and (kind, ref) not in bad_refs]
            if good_points:
                new_lat = statistics.median(p[0] for p in good_points)
                new_lon = statistics.median(p[1] for p in good_points)
                if not args.test:
                    await execute("UPDATE complexes SET lat = $2, lon = $3 WHERE id = $1", cid, new_lat, new_lon)
                report["after_complex_lat"], report["after_complex_lon"] = new_lat, new_lon
                recomputed += 1
            elif c["address"]:
                from bot.core.geo import geocode
                try:
                    coords = await geocode(c["address"], city="astana")
                except Exception as e:
                    coords = None
                    print(f"  geocode #{cid} failed: {e}")
                if coords:
                    if not args.test:
                        await execute("UPDATE complexes SET lat = $2, lon = $3 WHERE id = $1", cid, coords[0], coords[1])
                    report["after_complex_lat"], report["after_complex_lon"] = coords
                    recomputed += 1

        reports.append(report)

    print(f"{'[TEST, без записи] ' if args.test else ''}Карантин: complexes.lat/lon={quarantined_cx}, "
          f"homeportal_objects={quarantined_hp}, пересчитано complexes.lat/lon={recomputed}")
    print(f"Затронуто ЖК: {len(reports)}")
    print()
    for r in reports:
        print(f"#{r['complex_id']} {r['name']!r}:")
        print(f"  до:    complex.lat/lon = ({r['before_complex_lat']}, {r['before_complex_lon']})")
        print(f"  после: complex.lat/lon = ({r['after_complex_lat']}, {r['after_complex_lon']})")
        for kind, ref, lat, lon, dist in r["bad"]:
            label = f"homeportal #{ref}" if kind == "homeportal" else "сам complex"
            print(f"    карантин: {label} ({lat:.4f},{lon:.4f}) — {dist/1000:.0f} км от медианы")
        print()

    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
