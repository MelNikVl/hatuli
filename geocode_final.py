#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Финальная геокодировка: убираем ВЕСЬ префикс района целиком."""
import subprocess, re, sys, time, json, os, urllib.parse, urllib.request

def psql(sql):
    r = subprocess.run(['sudo', '-u', 'postgres', 'psql', '-d', 'krisha_bot', '-t', '-A',
                        '-F', chr(9), '-c', sql], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:300])
    return r.stdout.strip()

def clean_addr(a):
    if not a:
        return ''
    x = re.sub(r'\s*—.*$', '', a)
    x = re.sub(r'\s+[-–—]\s+.*$', '', x)
    # убрать ВЕСЬ префикс "Район р-н, " целиком
    x = re.sub(r'^[^,]*р-н\s*,?\s*', '', x, flags=re.I)
    x = re.sub(r'^[^,]*район\s*,?\s*', '', x, flags=re.I)
    x = re.sub(r'\b(ул\.?|улица|пр\.?|проспект|пер\.?|переулок)\s*', '', x, flags=re.I)
    x = re.sub(r'[«»"\']', '', x)
    x = re.sub(r'\s+', ' ', x).strip()
    return x

CP = '/tmp/geo_cp3.txt'
done = set()
if os.path.exists(CP):
    for l in open(CP, encoding='utf-8'):
        p = l.rstrip('\n').split('\t')
        if p:
            done.add(p[0])

# сироты без координат ИЛИ на фолбэке, с номером в адресе
FALLBACK = ("'51.1266,71.4228','51.1520,71.5009','51.1380,71.4687','51.1564,71.4737','51.1239,71.3932',"
            "'51.1533,71.3469','51.1210,71.5044','51.1482,71.3546','51.1413,71.4808','51.1401,71.3741',"
            "'51.1465,71.4101','51.1316,71.3945'")
rows = []
for l in psql(f"""
    SELECT id || chr(9) || COALESCE(address,'') || chr(9) || COALESCE(lat::text,'') || chr(9) || 'X'
    FROM apartment_listings WHERE complex_name IS NULL AND address ~ '[0-9]'
      AND (lat IS NULL OR round(lat::numeric,4) || ',' || round(lon::numeric,4) IN ({FALLBACK}))
""").splitlines():
    if not l:
        continue
    p = l.split('\t')
    if p[0] not in done:
        rows.append((p[0], p[1]))
print(f"к финальной обработке: {len(rows)}", flush=True)

UA = "hatuli-bot/1.0 (admin@hatuli.ai)"
ok = 0
with open(CP, 'a', encoding='utf-8') as f:
    for i, (lid, addr) in enumerate(rows):
        clean = clean_addr(addr)
        got = None
        if clean:
            q = urllib.parse.quote("Астана, " + clean)
            try:
                req = urllib.request.Request(
                    f"https://nominatim.openstreetmap.org/search?q={q}&format=json&limit=1&countrycodes=kz",
                    headers={'User-Agent': UA})
                with urllib.request.urlopen(req, timeout=8) as r:
                    d = json.loads(r.read().decode())
                if d:
                    got = d[0]
            except Exception:
                pass
        if got:
            f.write(f"{lid}\t{got['lat']}\t{got['lon']}\tnominatim3\t\n")
            ok += 1
        else:
            f.write(f"{lid}\t\t\tnotfound3\t\n")
        f.flush()
        if (i + 1) % 25 == 0:
            print(f"  ...{i+1}/{len(rows)} ok={ok}", flush=True)
        time.sleep(1.1)
print(f"финально геокодировано: {ok}/{len(rows)}", flush=True)

batch = []
for l in open(CP, encoding='utf-8'):
    p = l.rstrip('\n').split('\t')
    if len(p) >= 4 and p[3] == 'nominatim3' and p[1]:
        batch.append(f"('{p[0].replace(chr(39), chr(39)*2)}',{p[1]},{p[2]})")
        if len(batch) >= 300:
            psql(f"UPDATE apartment_listings AS a SET lat=v.lat, lon=v.lon FROM (VALUES {','.join(batch)}) AS v(id,lat,lon) WHERE a.id=v.id")
            batch = []
if batch:
    psql(f"UPDATE apartment_listings AS a SET lat=v.lat, lon=v.lon FROM (VALUES {','.join(batch)}) AS v(id,lat,lon) WHERE a.id=v.id")
print("координаты записаны", flush=True)
print(psql("SELECT COUNT(*) FILTER (WHERE lat IS NOT NULL) || '/' || COUNT(*) FROM apartment_listings WHERE complex_name IS NULL"), flush=True)
