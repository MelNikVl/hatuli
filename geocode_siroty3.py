#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Геокодирование сирот v3: пауза 2с + ретраи при 429. Итог: координаты = адрес; адрес -> ЖК."""
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

print("старт v3", flush=True)

# A) словарь: norm_addr -> (lat, lon, complex_name)
addr_to_cx = {}
raw = psql("""
    SELECT a.address || chr(9) || c.lat::text || chr(9) || c.lon::text || chr(9) || c.name || chr(9) || 'X'
    FROM apartment_listings a
    JOIN complexes c ON lower(trim(c.name)) = lower(trim(a.complex_name))
    WHERE a.address IS NOT NULL AND a.address != '' AND c.lat IS NOT NULL AND c.is_garbage IS NOT TRUE
""")
for l in raw.splitlines():
    if not l:
        continue
    p = l.split('\t')
    n = norm_addr(p[0])
    if len(n) >= 5 and n not in addr_to_cx:
        addr_to_cx[n] = (p[1], p[2], p[3])
print(f"адресов ЖК: {len(addr_to_cx)}", flush=True)

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

# A) адрес -> ЖК (привязка complex_name + координаты ЖК)
matched = 0
for s in siroty:
    n = norm_addr(s['addr'])
    if len(n) >= 5 and n in addr_to_cx:
        s['lat'], s['lon'], s['zhk'] = addr_to_cx[n][0], addr_to_cx[n][1], addr_to_cx[n][2]
        s['src'] = 'zhk_addr'
        matched += 1
print(f"A) адрес совпал с ЖК: {matched} — привяжем к ЖК", flush=True)

# B) остаток — Nominatim (пауза 2с, ретраи)
todo = [s for s in siroty if 'src' not in s and s['addr']]
print(f"B) на Nominatim: {len(todo)}", flush=True)

UA = "hatuli-bot/1.0 (admin@hatuli.ai)"
done, ok = 0, 0
for s in todo:
    q = urllib.parse.quote("Астана, " + s['addr'])
    got = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(
                f"https://nominatim.openstreetmap.org/search?q={q}&format=json&limit=1&countrycodes=kz",
                headers={'User-Agent': UA})
            with urllib.request.urlopen(req, timeout=8) as r:
                d = json.loads(r.read().decode())
            got = d
            break
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(3 + attempt * 2)
                continue
            print("ERR:", s['addr'][:40], e.code, flush=True)
            break
        except Exception as e:
            print("ERR:", s['addr'][:40], str(e)[:60], flush=True)
            break
    if got:
        s['lat'], s['lon'], s['src'] = got[0]['lat'], got[0]['lon'], 'nominatim'
        ok += 1
    else:
        s['src'] = 'notfound'
    done += 1
    if done % 25 == 0:
        print(f"  ...{done}/{len(todo)} ok={ok}", flush=True)
    time.sleep(2)
print(f"B) geocoded: {ok}", flush=True)

# ── запись ──
# 1) привязка к ЖК по адресу (complex_name)
zhk_upd = 0
for s in siroty:
    if s.get('src') == 'zhk_addr' and s.get('zhk'):
        psql(f"UPDATE apartment_listings SET complex_name = '{s['zhk'].replace(chr(39), chr(39)*2)}' WHERE id = {s['id']}")
        zhk_upd += 1
print(f"привязано к ЖК по адресу: {zhk_upd}", flush=True)

# 2) координаты для остальных (все, у кого есть lat)
batch = []
for s in siroty:
    if s.get('lat') and s.get('src') in ('zhk_addr', 'nominatim'):
        batch.append(f"({s['id']},{s['lat']},{s['lon']})")
        if len(batch) >= 300:
            psql(f"UPDATE apartment_listings AS a SET lat=v.lat, lon=v.lon FROM (VALUES {','.join(batch)}) AS v(id,lat,lon) WHERE a.id::text=v.id::text")
            batch = []
if batch:
    psql(f"UPDATE apartment_listings AS a SET lat=v.lat, lon=v.lon FROM (VALUES {','.join(batch)}) AS v(id,lat,lon) WHERE a.id::text=v.id::text")
print(f"координаты обновлены для {len([s for s in siroty if s.get('lat')])}", flush=True)

# итог
print(psql("SELECT COUNT(*) FILTER (WHERE lat IS NOT NULL) || '/' || COUNT(*) AS s_koord FROM apartment_listings WHERE complex_name IS NULL"), flush=True)
print(psql("SELECT COUNT(*) || ' сирот осталось' FROM apartment_listings WHERE complex_name IS NULL"), flush=True)
