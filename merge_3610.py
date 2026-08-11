#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Объединение дубля 3610 «Миллениум Парк» -> 2042 «Millenium Park» + ссылки источников."""
import subprocess, json

def psql(sql):
    r = subprocess.run(['sudo', '-u', 'postgres', 'psql', '-d', 'krisha_bot', '-t', '-A', '-c', sql],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:300])
    return r.stdout.strip()

# 1) homeportal object 76 -> 2042 (перенос привязки с 3610)
psql("UPDATE homeportal_objects SET matched_complex_id=2042 WHERE object_id=76")
print("homeportal 76 -> 2042")

# 2) фото из 3610 (3 homeportal-фото) добавить в 2042, если их нет
photos_2042 = psql("SELECT photos::text FROM complexes WHERE id=2042")
photos_3610 = psql("SELECT photos::text FROM complexes WHERE id=3610")
try:
    p42 = json.loads(photos_2042) if photos_2042 else []
    p10 = json.loads(photos_3610) if photos_3610 else []
    merged = list(p42)
    for p in p10:
        if p not in merged:
            merged.append(p)
    arr = json.dumps(merged[:10], ensure_ascii=False).replace("'", "''")
    psql(f"UPDATE complexes SET photos='{arr}'::jsonb WHERE id=2042")
    print(f"фото объединены: {len(p42)} + {len(p10)} -> {len(merged[:10])}")
except Exception as e:
    print("photos merge err:", e)

# 3) source_info 3610 -> 2042 (korter/homsters оттуда, если есть)
si = psql("SELECT COALESCE(source_info,'{}'::jsonb)::text FROM complexes WHERE id=3610")
data = {}
try:
    data = json.loads(si) if si else {}
except Exception:
    data = {}
data.setdefault("korter", {})
data["korter"]["url"] = "https://korter.kz/жк-millennium-park-астана"
data["korter"]["name"] = "Millennium Park"
data.setdefault("homsters", {})
data["homsters"]["url"] = "https://homsters.kz/bazis/millennium-park"
data["homsters"]["name"] = "Millennium Park"
arr = json.dumps(data, ensure_ascii=False).replace("'", "''")
psql(f"UPDATE complexes SET source_info='{arr}'::jsonb WHERE id=2042")
print("source_info: korter + homsters URL записаны в 2042")

# 4) 3610 -> garbage
psql("UPDATE complexes SET is_garbage=TRUE WHERE id=3610")
print("3610 -> garbage (дубль)")

# проверка
print(psql("SELECT c.id, c.name, c.source_info->'korter'->>'url' AS k, c.source_info->'homsters'->>'url' AS h FROM complexes c WHERE c.id=2042"))
print(psql("SELECT object_id, matched_complex_id FROM homeportal_objects WHERE object_id=76"))
