#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Фото из активных объявлений ЖК (первые фото первого объявления с фото)."""
import subprocess, json

def psql(sql):
    r = subprocess.run(['sudo', '-u', 'postgres', 'psql', '-d', 'krisha_bot', '-t', '-A', '-c', sql],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:300])
    return r.stdout.strip()

for cid, cname in [(1336, 'Бейбарыс'), (223, 'safar'), (2891, 'Tumar Exclusive'), (2807, 'Вдоль Ручья Сарыбулак')]:
    # берём фото из 2-3 активных объявлений (первые 5 фото каждого)
    rows = psql(f"""SELECT a.photos FROM apartment_listings a
                    WHERE lower(trim(a.complex_name)) = lower(trim('{cname}'))
                      AND a.is_active IS NOT FALSE AND a.photos IS NOT NULL
                      AND a.photos::text != '[]' AND a.photos::text LIKE '%krisha%'
                    ORDER BY a.last_seen DESC NULLS LAST LIMIT 3""").splitlines()
    photos = []
    for r in rows:
        if not r:
            continue
        try:
            arr = json.loads(r)
            if isinstance(arr, list):
                for p in arr:
                    if isinstance(p, str) and p.startswith('http') and p not in photos:
                        photos.append(p)
        except Exception:
            pass
    photos = photos[:10]
    if photos:
        arr = json.dumps(photos, ensure_ascii=False).replace("'", "''")
        psql(f"UPDATE complexes SET photos = '{arr}'::jsonb WHERE id = {cid}")
        print(f"  {cid} {cname}: {len(photos)} фото из объявлений")
    else:
        print(f"  {cid} {cname}: фото не найдены")
