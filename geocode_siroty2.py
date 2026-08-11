#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Шаг 1: перегеокодирование сирот. A) матч с ЖК по адресу, B) Nominatim."""
import subprocess, re, sys, time, json, urllib.parse, urllib.request

def norm_addr(a):
    if not a:
        return ''
    x = a.lower()
    x = re.sub(r'\s*—.*$', '', x)
    x = re.sub(r'\s+[-–—]\s+.*$', '', x)
    x = re.sub(r'[«»"\']', '', x)
    x = re.sub(r'\b(р-н|район|р н|аудан)\b', '', x)
    x = re.sub(r'\b(ул\.?|улица|пр\.?|проспект|пер\.?|переулок|шоссе)\b', '', x)
    x = re.sub(r'\b(мкр\.?|микрорайон|мкр-н)\b', ' мкр', x)
    x = re.sub(r'\b(город|г\.?)\b', '', x)
    x = re.sub(r'[.,;:()]', ' ', x)
    x = re.sub(r'\s+', ' ', x).strip()
    return x

def psql(sql):
    r = subprocess.run(['sudo', '-u', 'postgres', 'psql', '-d', 'krisha_bot', '-t', '-A',
                        '-F', chr(9), '-c', sql], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:300])
    return r.stdout.strip()

print("старт", flush=True)

# A) словарь адрес -> координаты ЖК
addr_to_cx = {}
raw = psql("""
    SELECT a.address || chr(9) || c.lat::text || chr(9) || c.lon::text || chr(9) || 'X'
    FROM apartment_listings a
    JOIN complexes c ON lower(trim(c.name)) = lower(trim(a.complex_name))
    WHERE a.address IS NOT NULL AND a.address != '' AND c.lat IS NOT NULL AND c.is_garbage IS NOT TRUE
""")
print("словарь ЖК: строк", len(raw.splitlines()), flush=True)
for l in raw.splitlines():
    if not l:
        continue
    p = l.split('\t')
    n = norm_addr(p[0])
    if len(n) >= 5 and n not in addr_to_cx:
        addr_to_cx[n] = (p[1], p[2])
print(f"уникальных адресов ЖК: {len(addr_to_cx)}", flush=True)

# сироты
raw2 = psql("""
    SELECT id::text || chr(9) || COALESCE(address,'') || chr(9) || COALESCE(lat::text,'') || chr(9) || COALESCE(lon::text,'') || chr(9) || 'X'
    FROM apartment_listings WHERE complex_name IS NULL
""")
siroty = []
for l in raw2.splitlines():
    if not l:
        continue
    p = l.split('\t')
    siroty.append({'id': p[0], 'addr': p[1], 'lat': p[2], 'lon': p[3]})
print(f"сирот: {len(siroty)}", flush=True)

# A) матч по адресам ЖК
matched = 0
for s in siroty:
    n = norm_addr(s['addr'])
    if len(n) >= 5 and n in addr_to_cx:
        s['lat'], s['lon'], s['src'] = addr_to_cx[n][0], addr_to_cx[n][1], 'zhk_addr'
        matched += 1
print(f"A) закрыто адресами ЖК: {matched}", flush=True)

# B) Nominatim
todo = [s for s in siroty if 'src' not in s and s['addr']]
print(f"B) на Nominatim: {len(todo)}", flush=True)

UA = "hatuli-bot/1.0 (admin@hatuli.ai)"
done = 0
ok = 0
for s in todo:
    q = urllib.parse.quote("Астана, " + s['addr'])
    try:
        req = urllib.request.Request(
            f"https://nominatim.openstreetmap.org/search?q={q}&format=json&limit=1&countrycodes=kz",
            headers={'User-Agent': UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read().decode())
        if d:
            s['lat'], s['lon'], s['src'] = d[0]['lat'], d[0]['lon'], 'nominatim'
            ok += 1
        else:
            s['src'] = 'notfound'
    except Exception as e:
        s['src'] = 'err'
        print("ERR:", s['addr'][:40], str(e)[:80], flush=True)
    done += 1
    if done % 50 == 0:
        print(f"  ...{done}/{len(todo)} ok={ok}", flush=True)
    time.sleep(1.1)
print(f"B) geocoded: {ok}", flush=True)

# запись
upd = 0
batch = []
for s in siroty:
    if s.get('src') in ('zhk_addr', 'nominatim'):
        batch.append(f"({s['id']},{s['lat']},{s['lon']})")
        if len(batch) >= 300:
            psql(f"UPDATE apartment_listings AS a SET lat=v.lat, lon=v.lon FROM (VALUES {','.join(batch)}) AS v(id,lat,lon) WHERE a.id::text=v.id::text")
            upd += len(batch)
            batch = []
if batch:
    psql(f"UPDATE apartment_listings AS a SET lat=v.lat, lon=v.lon FROM (VALUES {','.join(batch)}) AS v(id,lat,lon) WHERE a.id::text=v.id::text")
    upd += len(batch)
print(f"\nобновлено координат: {upd}", flush=True)
print(psql("SELECT COUNT(*) FILTER (WHERE lat IS NOT NULL) || '/' || COUNT(*) FROM apartment_listings WHERE complex_name IS NULL"), flush=True)
