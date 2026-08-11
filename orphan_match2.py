#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Фаззи-матч сирот (complex_name IS NULL) с ЖК: адрес-префикс + расстояние < 300м."""
import subprocess, re, sys, math
sys.path.insert(0, '/home/nik/krisha_bot')

'
P = chr(39) + chr(37) + chr(39)   # '%'

def psql(sql):
    r = subprocess.run(['sudo', '-u', 'postgres', 'psql', '-d', 'krisha_bot', '-t', '-A', '-F', chr(9), '-c', sql],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:300])
    return r.stdout.strip()

def norm_addr(a):
    a = a.lower()
    a = re.sub(r'р-н[а-яё]*\s*', '', a)
    a = re.sub(r'[\u2014\u2013-].*$', '', a)
    a = re.sub(r'[^a-zа-я0-9 ]', ' ', a)
    a = re.sub(r'\s+', ' ', a).strip()
    return a

def dist(lat1, lon1, lat2, lon2):
    if not lat1 or not lat2 or not lon1 or not lon2:
        return 9999
    R = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return R * 2 * math.asin(math.sqrt(a))

# 1) живые ЖК + типичный адрес
sql1 = ("SELECT c.id, c.name, c.lat, c.lon, "
        "(SELECT a.address FROM apartment_listings a "
        " WHERE lower(trim(a.complex_name)) = lower(trim(c.name)) "
        "   AND a.address IS NOT NULL AND length(a.address) > 3 "
        " ORDER BY (a.address LIKE " + P + " || c.name || " + P + ") DESC, a.id LIMIT 1) AS addr "
        "FROM complexes c WHERE is_garbage IS NOT TRUE")
rows = psql(sql1).splitlines()
zhk = []
for r in rows:
    if not r:
        continue
    p = r.split('\t')
    if len(p) < 5:
        continue
    try:
        zhk.append({'id': int(p[0]), 'name': p[1],
                    'lat': float(p[2]) if p[2] else None,
                    'lon': float(p[3]) if p[3] else None,
                    'addr': p[4]})
    except ValueError:
        pass
print(f"ЖК с адресами: {len(zhk)}")

# 2) сироты
rows2 = psql("SELECT id, address, lat, lon FROM apartment_listings "
             "WHERE complex_name IS NULL AND lat IS NOT NULL AND lon IS NOT NULL "
             "AND address IS NOT NULL AND length(address) > 3").splitlines()
print(f"Сирот с координатами и адресом: {len(rows2)}")

# 3) матчинг
matched = 0
for r in rows2:
    if not r:
        continue
    p = r.split('\t')
    if len(p) < 4:
        continue
    lid, addr, lat, lon = p[0], p[1], float(p[2]), float(p[3])
    na = norm_addr(addr)
    if len(na) < 6:
        continue
    best = None
    for z in zhk:
        if not z['addr']:
            continue
        nz = norm_addr(z['addr'])
        if not nz or len(nz) < 6:
            continue
        if na[:6] == nz[:6] and dist(lat, lon, z['lat'], z['lon']) < 300:
            best = z
            break
    if best:
        psql(f"UPDATE apartment_listings SET complex_name = '{best['name'].replace(chr(39), chr(39)*2)}' WHERE id = {lid}")
        matched += 1

print(f"Привязано сирот: {matched}")
print(psql("SELECT COUNT(*) || ' осталось сирот' FROM apartment_listings WHERE complex_name IS NULL"))
