#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""2042 Millenium Park: korter/homsters URL в source_info + привязка homeportal (object 76)."""
import subprocess, json

def psql(sql):
    r = subprocess.run(['sudo', '-u', 'postgres', 'psql', '-d', 'krisha_bot', '-t', '-A', '-c', sql],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:300])
    return r.stdout.strip()

# 1) source_info: korter + homsters
si = psql("SELECT COALESCE(source_info, '{}'::jsonb)::text FROM complexes WHERE id=2042")
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
psql(f"UPDATE complexes SET source_info = '{arr}'::jsonb WHERE id=2042")
print("source_info: korter + homsters записаны")

# 2) homeportal object 76 -> matched_complex_id 2042
n = psql("SELECT COUNT(*) FROM homeportal_objects WHERE object_id=76 AND matched_complex_id=2042")
if n == "0":
    psql("UPDATE homeportal_objects SET matched_complex_id=2042 WHERE object_id=76")
    print("homeportal object 76 -> matched_complex_id=2042")
else:
    print("homeportal уже привязан")

# проверка
print(psql("SELECT c.id, c.source_info->'korter'->>'url' AS k, c.source_info->'homsters'->>'url' AS h FROM complexes c WHERE c.id=2042"))
print(psql("SELECT object_id, matched_complex_id FROM homeportal_objects WHERE object_id=76"))
