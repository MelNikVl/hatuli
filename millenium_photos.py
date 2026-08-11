#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Фото для Millenium Park (2042): галерея Крыши + homeportal."""
import subprocess, re, json

def psql(sql):
    r = subprocess.run(['sudo', '-u', 'postgres', 'psql', '-d', 'krisha_bot', '-t', '-A', '-c', sql],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:300])
    return r.stdout.strip()

html = open('/tmp/kmp.html', encoding='utf-8', errors='ignore').read()

# 1) полноразмерные фото из блока "photos": (complex/...)
photos = re.findall(r'https://krisha-photos\.kcdn\.online/complex/[^"\']+\.(?:png|jpg|jpeg)', html)
# 2) галерея 750x470
photos += re.findall(r'https://krisha-photos\.kcdn\.online/[^"\']+photo-750x470\.(?:jpg|jpeg|png)', html)
# дедуп
out = []
for p in photos:
    if p not in out:
        out.append(p)

# 3) homeportal фото
hp = psql("SELECT images FROM homeportal_objects WHERE object_id=76")
try:
    hp_arr = json.loads(hp)
    for im in hp_arr:
        link = im.get("image_link")
        if link and link not in out:
            out.append(link)
except Exception:
    pass

out = out[:10]
print(f"Фото собрано: {len(out)}")
for p in out:
    print(" ", p)

arr = json.dumps(out, ensure_ascii=False).replace("'", "''")
psql(f"UPDATE complexes SET photos = '{arr}'::jsonb WHERE id = 2042")
print("Записано в complexes.photos (2042)")

# заодно: застройщик BAZIS-А
psql("UPDATE complexes SET developer_id = (SELECT id FROM developers WHERE name ILIKE '%bazis%' LIMIT 1) WHERE id = 2042")
print("Застройщик: BAZIS-А")
