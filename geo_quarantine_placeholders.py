#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Карантин координат, класс 4 — placeholder-значения (задача 2026-08-12,
после geo_quarantine.py нашли, что часть "разъезда блоков" в аудите blob-
комплексов — не реальные разные точки, а один и тот же STUB-координатный
компонент homeportal у РАЗНЫХ, никак не связанных ЖК (напр. долгота
71.279295 — у 4 разных complex_id, широта 51.069017 — у 7). Похоже на
district-заглушку для объектов без точного геокодинга на стороне
источника, а не на реальную точку. В отличие от geo_quarantine.py (точка
далеко от медианы её ЖК), тут сигнал другой: ось координаты СОВПАДАЕТ
БУКВАЛЬНО у ≥3 никак не связанных между собой ЖК — то есть значение,
скорее всего, вообще не про конкретный объект.

Метод: широта ИЛИ долгота встречается у >=3 РАЗНЫХ matched_complex_id ->
вся точка (обе координаты) этой строки в карантин (значение, не строка
целиком) — раз хотя бы одна ось не своя, доверять второй оси тоже нельзя
(неизвестно, реальна ли она сама по себе или это координата другого
объекта, у которого совпала другая ось). complexes.lat/lon, если сама
равна одному из этих значений — туда же, с пересчётом из выживших
доверенных связей / re-geocode (тот же паттерн, что в geo_quarantine.py).

Запуск: venv/bin/python geo_quarantine_placeholders.py [--test]
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
MIN_DISTINCT_COMPLEXES = 3


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()

    from bot.db.pg import init_pool, close_pool, fetch, execute

    await init_pool(DATABASE_URL)

    bad_lats = {r["latitude"] for r in await fetch("""
        SELECT latitude, count(DISTINCT matched_complex_id) AS n
        FROM homeportal_objects WHERE latitude IS NOT NULL AND matched_complex_id IS NOT NULL
        GROUP BY latitude HAVING count(DISTINCT matched_complex_id) >= $1
    """, MIN_DISTINCT_COMPLEXES)}
    bad_lons = {r["longitude"] for r in await fetch("""
        SELECT longitude, count(DISTINCT matched_complex_id) AS n
        FROM homeportal_objects WHERE longitude IS NOT NULL AND matched_complex_id IS NOT NULL
        GROUP BY longitude HAVING count(DISTINCT matched_complex_id) >= $1
    """, MIN_DISTINCT_COMPLEXES)}
    print(f"placeholder-широты (>= {MIN_DISTINCT_COMPLEXES} разных ЖК): {sorted(bad_lats)}")
    print(f"placeholder-долготы (>= {MIN_DISTINCT_COMPLEXES} разных ЖК): {sorted(bad_lons)}")

    rows = await fetch("""
        SELECT object_id, matched_complex_id, name, latitude, longitude
        FROM homeportal_objects
        WHERE matched_complex_id IS NOT NULL AND geo_quarantined_at IS NULL
          AND (latitude = ANY($1::text[]) OR longitude = ANY($2::text[]))
    """, list(bad_lats), list(bad_lons))
    print(f"\nстрок homeportal_objects в карантин: {len(rows)}")
    for r in rows:
        which = []
        if r["latitude"] in bad_lats:
            which.append("lat")
        if r["longitude"] in bad_lons:
            which.append("lon")
        print(f"  #{r['object_id']:5} (complex {r['matched_complex_id']}) {r['name'][:40]!r:42} "
              f"({r['latitude']},{r['longitude']}) — placeholder: {'+'.join(which)}")
        if not args.test:
            await execute("""
                UPDATE homeportal_objects SET
                    geo_quarantined_lat = latitude, geo_quarantined_lon = longitude,
                    latitude = NULL, longitude = NULL,
                    geo_quarantined_at = now(),
                    geo_quarantine_reason = 'placeholder-координата (' || $2 || '), совпадает у >=3 разных ЖК'
                WHERE object_id = $1
            """, r["object_id"], "+".join(which))

    # complexes.lat/lon, если сама placeholder — пересчёт из доверенных
    # (уже НЕ закарантиненных) homeportal-точек того же ЖК, иначе re-geocode.
    # Сравнение по близости (не точное строковое) — complexes.lat/lon это
    # real (одинарная точность), у homeportal.latitude/longitude (TEXT из
    # API) может отличаться в последнем знаке при том же физическом
    # значении; 0.0001° ~ 11 м — заведомо меньше разрешения, на котором
    # вообще имеет смысл говорить "то же самое значение".
    EPS = 0.0001
    bad_lats_f = {float(v) for v in bad_lats if v}
    bad_lons_f = {float(v) for v in bad_lons if v}
    affected_complex_ids = sorted({r["matched_complex_id"] for r in rows})
    recomputed = 0
    for cid in affected_complex_ids:
        cx = await fetch("SELECT id, name, lat, lon, address FROM complexes WHERE id = $1", cid)
        if not cx:
            continue
        cx = cx[0]
        is_bad_pair = (cx["lat"] is not None and any(abs(cx["lat"] - v) < EPS for v in bad_lats_f)) or \
                      (cx["lon"] is not None and any(abs(cx["lon"] - v) < EPS for v in bad_lons_f))
        if not is_bad_pair:
            continue
        print(f"\ncomplex #{cid} {cx['name']!r}: свой lat/lon тоже placeholder-подозрителен "
              f"({cx['lat']},{cx['lon']})")
        good = await fetch("""
            SELECT latitude::float AS lat, longitude::float AS lon FROM homeportal_objects
            WHERE matched_complex_id = $1 AND geo_quarantined_at IS NULL
              AND latitude IS NOT NULL AND longitude IS NOT NULL
        """, cid)
        if good:
            new_lat = statistics.median(g["lat"] for g in good)
            new_lon = statistics.median(g["lon"] for g in good)
            print(f"  пересчёт из {len(good)} доверенных точек -> ({new_lat:.6f}, {new_lon:.6f})")
            if not args.test:
                await execute("""
                    UPDATE complexes SET
                        geo_quarantined_lat = lat, geo_quarantined_lon = lon,
                        lat = $2, lon = $3,
                        geo_quarantined_at = now(),
                        geo_quarantine_reason = 'placeholder-координата, совпадает у >=3 разных ЖК; пересчитано из доверенных точек'
                    WHERE id = $1
                """, cid, new_lat, new_lon)
            recomputed += 1
        elif cx["address"]:
            from bot.core.geo import geocode
            try:
                coords = await geocode(cx["address"], city="astana")
            except Exception as e:
                coords = None
                print(f"  geocode failed: {e}")
            if coords:
                print(f"  re-geocode по адресу -> {coords}")
                if not args.test:
                    await execute("""
                        UPDATE complexes SET
                            geo_quarantined_lat = lat, geo_quarantined_lon = lon,
                            lat = $2, lon = $3,
                            geo_quarantined_at = now(),
                            geo_quarantine_reason = 'placeholder-координата, совпадает у >=3 разных ЖК; re-geocode по адресу'
                        WHERE id = $1
                    """, cid, coords[0], coords[1])
                recomputed += 1
            else:
                print("  re-geocode не дал результата — lat/lon остаются как есть (не карантиню без замены)")

    print(f"\n{'[TEST] ' if args.test else ''}ИТОГ: карантин связей={len(rows)}, "
          f"complexes.lat/lon пересчитано={recomputed}")

    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
