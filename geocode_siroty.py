#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Шаг 1: перегеокодирование сирот.
A) матч по нормализованному адресу с ЖК (координаты ЖК)
B) остаток — Nominatim (1 запрос/с, в фоне)
"""
import subprocess, re, sys, time, json, urllib.parse, urllib.request
sys.path.insert(0, '/home/nik/krisha_bot')
from house_years_0 import norm_addr

def psql(sql):
    r = subprocess.run(['sudo', '-u', 'postgres', 'psql', '-d', 'krisha_bot', '-t', '-A',
                        '-F', chr(9), '-c', sql], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:300])
    return r.stdout.strip()

# ── A) адрес -> координаты ЖК ──
# собрать все адреса объявлений, привязанных к ЖК, нормализовать, взять координаты ЖК
addr_to_cx = {}  # norm_addr -> (lat, lon)
for l in psql("""
    SELECT a.address || chr(9) || c.lat::text || chr(9) || c.lon::text || chr(9) || c.name || chr(9) || 'X'
    FROM apartment_listings a
    JOIN complexes c ON lower(trim(c.name)) = lower(trim(a.complex_name))
    WHERE a.address IS NOT NULL AND a.address != '' AND c.lat IS NOT NULL AND c.is_garbage IS NOT TRUE
""").splitlines():
    if not l:
        continue
    p = l.split('\t')
    while len(p) < 5:
        p.append('')
    n = norm_addr(p[0])
    if len(n) >= 5 and n not in addr_to_cx:
        addr_to_cx[n] = (p[1], p[2])

print(f"Адресов ЖК в словаре: {len(addr_to_cx)}")

# ── сироты ──
siroty = []
for l in psql("""
    SELECT id::text || chr(9) || COALESCE(address,'') || chr(9) || COALESCE(lat::text,'') || chr(9) || COALESCE(lon::text,'') || chr(9) || 'X'
    FROM apartment_listings WHERE complex_name IS NULL
""").splitlines():
    if not l:
        continue
    p = l.split('\t')
    while len(p) < 5:
        p.append('')
    siroty.append({'id': p[0], 'addr': p[1], 'lat': p[2], 'lon': p[3]})

print(f"Сирот всего: {len(siroty)}")

# A) матч по адресам ЖК
matched = 0
for s in siroty:
    n = norm_addr(s['addr'])
    if len(n) >= 5 and n in addr_to_cx:
        s['lat'] = addr_to_cx[n][0]
        s['lon'] = addr_to_cx[n][1]
        s['src'] = 'zhk_addr'
        matched += 1
print(f"A) закрыто матчем по адресам ЖК: {matched}")

# B) остаток — Nominatim
todo = [s for s in siroty if 'src' not in s and s['addr']]
print(f"B) на Nominatim: {len(todo)}")

UA = "hatuli-bot/1.0 (admin@hatuli.ai)"
done = 0
with open('/tmp/geocoded.txt', 'w') as f:
    for s in todo:
        q = urllib.parse.quote("Астана, " + s['addr'])
        try:
            req = urllib.request.Request(
                f"https://nominatim.openstreetmap.org/search?q={q}&format=json&limit=1&countrycodes=kz",
                headers={'User-Agent': UA})
            with urllib.request.urlopen(req, timeout=15) as r:
                d = json.loads(r.read().decode())
            if d:
                s['lat'] = d[0]['lat']
                s['lon'] = d[0]['lon']
                s['src'] = 'nominatim'
            else:
                s['src'] = 'notfound'
        except Exception as e:
            s['src'] = 'err'
            print("ERR:", s['addr'][:40], e)
        done += 1
        if s['src'] in ('nominatim', 'zhk_addr'):
            f.write(f"{s['id']}\t{s['lat']}\t{s['lon']}\t{s['src']}\n")
        if done % 50 == 0:
            print(f"  ...{done}/{len(todo)}")
        time.sleep(1.1)

# ── запись в БД ──
upd = 0
for s in siroty:
    if s.get('src') in ('zhk_addr', 'nominatim'):
        psql(f"UPDATE apartment_listings SET lat={s['lat']}, lon={s['lon']} WHERE id={s['id']}")
        upd += 1
print(f"\nОбновлено координат: {upd}")
print(psql("SELECT COUNT(*) FILTER (WHERE lat IS NOT NULL) || '/' || COUNT(*) FROM apartment_listings WHERE complex_name IS NULL"))
