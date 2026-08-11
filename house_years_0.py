#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Шаг 0: консолидация года постройки из объявлений (включая сирот) в house_years."""
import subprocess, re, statistics, sys
sys.path.insert(0, '/home/nik/krisha_bot')

def psql(sql):
    r = subprocess.run(['sudo', '-u', 'postgres', 'psql', '-d', 'krisha_bot', '-t', '-A',
                        '-F', chr(9), '-c', sql], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:300])
    return r.stdout.strip()

def norm_addr(a):
    if not a:
        return ''
    x = a.lower()
    x = re.sub(r'\s*—.*$', '', x)          # хвост после тире
    x = re.sub(r'\s+[-–—]\s+.*$', '', x)   # хвост после дефиса
    x = re.sub(r'[«»"\']', '', x)
    x = re.sub(r'\b(р-н|район|р н|аудан)\b', '', x)          # район
    x = re.sub(r'\b(ул\.?|улица|пр\.?|проспект|пер\.?|переулок|шоссе)\b', '', x)
    x = re.sub(r'\b(мкр\.?|микрорайон|мкр-н)\b', ' мкр', x)  # микрорайон -> мкр
    x = re.sub(r'\b(город|г\.?|г)\b', '', x)
    x = re.sub(r'[.,;:()]', ' ', x)
    x = re.sub(r'\s+', ' ', x).strip()
    return x

# 1) создать таблицу
psql("""CREATE TABLE IF NOT EXISTS house_years (
    address TEXT PRIMARY KEY,
    year_built INT,
    listings_cnt INT,
    source TEXT DEFAULT 'krisha_listings'
)""")
print("house_years создана/существует")

# 2) собрать адрес -> [годы]
rows = []
for l in psql(
    "SELECT COALESCE(address,'') || chr(9) || COALESCE(year_built::text,'') || chr(9) || 'X' "
    "FROM apartment_listings WHERE address IS NOT NULL AND address != ''").splitlines():
    if not l:
        continue
    p = l.split('\t')
    while len(p) < 3:
        p.append('')
    rows.append((p[0], p[1]))

groups = {}
for addr, yb in rows:
    if not yb:
        continue
    n = norm_addr(addr)
    if len(n) < 5:
        continue
    groups.setdefault(n, []).append(int(yb))

print(f"Уникальных нормализованных адресов с годом: {len(groups)}")

# 3) записать: медиана года
n_written = 0
batch = []
for addr, years in groups.items():
    med = int(statistics.median(years))
    batch.append((addr, med, len(years)))
    if len(batch) >= 500:
        vals = ','.join(f"('{a.replace(chr(39), chr(39)*2)}',{y},{c})" for a, y, c in batch)
        psql(f"INSERT INTO house_years (address, year_built, listings_cnt) VALUES {vals} "
             f"ON CONFLICT (address) DO UPDATE SET year_built = EXCLUDED.year_built, "
             f"listings_cnt = EXCLUDED.listings_cnt")
        n_written += len(batch)
        batch = []
if batch:
    vals = ','.join(f"('{a.replace(chr(39), chr(39)*2)}',{y},{c})" for a, y, c in batch)
    psql(f"INSERT INTO house_years (address, year_built, listings_cnt) VALUES {vals} "
         f"ON CONFLICT (address) DO UPDATE SET year_built = EXCLUDED.year_built, "
         f"listings_cnt = EXCLUDED.listings_cnt")
    n_written += len(batch)

print(f"Записано адресов: {n_written}")
print(psql("SELECT COUNT(*) FROM house_years"))
