#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Фаззи-матч объявлений без ЖК (complex_name IS NULL) по адресам/координатам с 1859 ЖК.
Только точные совпадения: адрес ЖК (из объявлений ЖК) vs адрес сироты + расстояние < 300м."""
import subprocess, sys, re, math
sys.path.insert(0, '/home/nik/krisha_bot')

def psql(sql):
    r = subprocess.run(['sudo', '-u', 'postgres', 'psql', '-d', 'krisha_bot', '-t', '-A', '-c', sql],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:300])
    return r.stdout.strip()

def norm_addr(a):
    a = a.lower()
    a = re.sub(r'р-н[а-яё]*\s*', '', a)          # район
    a = re.sub(r'[—–-].*$', '', a)               # хвост после тире
    a = re.sub(r'[^a-zа-я0-9 ]', ' ', a)
    a = re.sub(r'\s+', ' ', a).strip()
    return a

def dist(lat1, lon1, lat2, lon2):
    if not lat1 or not lat2: return 9999
    R = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1); dl = math.radians(lon2 - lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return R * 2 * math.asin(math.sqrt(a))

# 1) все живые ЖК с их типичными адресами (из объявлений ЖК) и координатами
zhk = []
rows = psql("""
    SELECT c.id, c.name, c.lat, c.lon,
           (SELECT a.address FROM apartment_listings a
            WHERE lower(trim(a.complex_name)) = lower(trim(c.name))
              AND a.address IS NOT NULL AND a.address != ''
            ORDER BY (a.address LIKE '%' || c.name || '%') DESC, a.id LIMIT 1) AS addr
    FROM complexes c WHERE is_garbage IS NOT TRUE
""").splitlines()
for r in rows:
    if not r: continue
    p = r.split('\t')
    if len(p) < 5: continue
    try:
        zhk.append({'id': int(p[0]), 'name': p[1], 'lat': float(p[2]) if p[2] else None,
                    'lon': float(p[3]) if p[3] else None, 'addr': p[4]})
    except ValueError:
        pass

print(f"ЖК с адресами: {len(zhk)}")

# 2) сироты: объявления без complex_name, с координатами
orphans = []
rows = psql("""
    SELECT id, address, lat, lon FROM apartment_listings
    WHERE complex_name IS NULL AND lat IS NOT NULL AND lon IS NOT NULL
      AND address IS NOT NULL AND address != ''
""").splitlines()
print(f"Сирот с координатами и адресом: {len(rows)}")

# 3) матчинг: нормализованный адрес сироты == префикс адреса ЖК + расстояние < 300м
matched = 0
for r in rows:
    if not r: continue
    p = r.split('\t')
    if len(p) < 4: continue
    lid, addr, lat, lon = p[0], p[1], float(p[2]), float(p[3])
    na = norm_addr(addr)
    if len(na) < 6: continue
    best = None
    for z in zhk:
        if not z['addr']: continue
        nz = norm_addr(z['addr'])
        if not nz: continue
        # адрес ЖК начинается с адреса сироты (или наоборот) по первым 6+ символам
        if na[:6] == nz[:6] and dist(lat, lon, z['lat'], z['lon']) < 300:
            best = z
            break
    if best:
        psql(f"UPDATE apartment_listings SET complex_name = '{best['name'].replace(chr(39), chr(39)*2)}' WHERE id = {lid}")
        matched += 1

print(f"Привязано сирот: {matched}")
print(psql("SELECT COUNT(*) || ' осталось сирот' FROM apartment_listings WHERE complex_name IS NULL"))
