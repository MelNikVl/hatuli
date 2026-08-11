#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""2110 Monaco Residence: ссылки korter/homsters + homeportal MONACO -> 2110, GRAND MONACO -> 2889."""
import subprocess, json

def psql(sql):
    r = subprocess.run(['sudo', '-u', 'postgres', 'psql', '-d', 'krisha_bot', '-t', '-A', '-c', sql],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:300])
    return r.stdout.strip()

# 1) homeportal: MONACO (пятна 1-6) -> 2110; GRAND MONACO -> 2889
psql("UPDATE homeportal_objects SET matched_complex_id=2110 WHERE object_id IN (530, 538, 542, 679, 774)")
psql("UPDATE homeportal_objects SET matched_complex_id=2889 WHERE object_id IN (1067, 1388, 1334)")
print("homeportal: MONACO -> 2110, GRAND MONACO -> 2889")

# 2) source_info 2110: korter + homsters
si = psql("SELECT COALESCE(source_info, '{}'::jsonb)::text FROM complexes WHERE id=2110")
try:
    data = json.loads(si) if si else {}
except Exception:
    data = {}
data.setdefault("korter", {})
data["korter"]["url"] = "https://korter.kz/жк-monaco-астана"
data["korter"]["name"] = "Monaco Residence"
data.setdefault("homsters", {})
data["homsters"]["url"] = "https://homsters.kz/bazis/monaco1"
data["homsters"]["name"] = "Monaco Residence"
arr = json.dumps(data, ensure_ascii=False).replace("'", "''")
psql(f"UPDATE complexes SET source_info = '{arr}'::jsonb WHERE id=2110")
print("source_info: korter + homsters записаны")

# 3) застройщик BAZIS-А (71)
psql("UPDATE complexes SET developer_id=71 WHERE id=2110")
print("developer: BAZIS-А (71)")

# проверка
print(psql("SELECT c.id, c.source_info->'korter'->>'url', c.source_info->'homsters'->>'url' FROM complexes c WHERE c.id=2110"))
print(psql("SELECT object_id, matched_complex_id FROM homeportal_objects WHERE object_id IN (530, 679, 1067, 1334) ORDER BY object_id"))
