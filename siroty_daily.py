#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ежедневный разбор сирот (крон 05:00):
1. Привязка к ЖК по полному совпадению адреса (norm_addr ∈ адресам ЖК)
2. Геокодирование новых сирот без точных координат через Nominatim
3. Лог в /tmp/siroty_daily.log
"""
import subprocess, re, sys, time, json, urllib.parse, urllib.request

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

def clean_addr(a):
    """Чистый адрес для Nominatim: без района, без хвостов."""
    if not a:
        return ''
    x = re.sub(r'\s*—.*$', '', a)
    x = re.sub(r'\s+[-–—]\s+.*$', '', x)
    x = re.sub(r'^[^,]*р-н\s*,?\s*', '', x, flags=re.I)
    x = re.sub(r'^[^,]*район\s*,?\s*', '', x, flags=re.I)
    x = re.sub(r'\b(ул\.?|улица|пр\.?|проспект|пер\.?|переулок)\s*', '', x, flags=re.I)
    x = re.sub(r'[«»"\']', '', x)
    x = re.sub(r'\s+', ' ', x).strip()
    return x

# фолбэк-точки (центры районов — координаты-заглушки)
FALLBACK = ("'51.1266,71.4228','51.1520,71.5009','51.1380,71.4687','51.1564,71.4737','51.1239,71.3932',"
            "'51.1533,71.3469','51.1210,71.5044','51.1482,71.3546','51.1413,71.4808','51.1401,71.3741',"
            "'51.1465,71.4101','51.1316,71.3945','51.1670,71.4270','51.1095,71.4550'")

t0 = time.time()
print("== сироты daily ==", flush=True)

# 1) словарь адрес -> ЖК (для привязки)
addr_to_cx = {}
raw = psql("""
    SELECT a.address || chr(9) || c.name || chr(9) || 'X'
    FROM apartment_listings a
    JOIN complexes c ON lower(trim(c.name)) = lower(trim(a.complex_name))
    WHERE a.address IS NOT NULL AND a.address != '' AND c.is_garbage IS NOT TRUE
""")
for l in raw.splitlines():
    if not l:
        continue
    p = l.split('\t')
    n = norm_addr(p[0])
    if len(n) >= 5 and n not in addr_to_cx:
        addr_to_cx[n] = p[1]
print(f"адресов ЖК: {len(addr_to_cx)}", flush=True)

# 2) сироты: привязка по адресу
rows = []
for l in psql("""
    SELECT id || chr(9) || COALESCE(address,'') || chr(9) || 'X'
    FROM apartment_listings WHERE complex_name IS NULL AND address IS NOT NULL AND address != ''
""").splitlines():
    if not l:
        continue
    p = l.split('\t')
    rows.append((p[0], p[1]))

zhk_linked = 0
batch = []
linked = []
for lid, addr in rows:
    n = norm_addr(addr)
    if len(n) >= 5 and n in addr_to_cx:
        linked.append((lid, addr_to_cx[n]))
        zhk_linked += 1
# батч-привязка по списку (id, имя_жк)
batch = []
for lid, cxname in linked:
    batch.append((lid, cxname))
    if len(batch) >= 200:
        vals = ','.join(f"('{a}', '{b.replace(chr(39), chr(39)*2)}')" for a, b in batch)
        psql(f"UPDATE apartment_listings SET complex_name = v.cx FROM (VALUES {vals}) AS v(id, cx) WHERE apartment_listings.id = v.id")
        batch = []
if batch:
    vals = ','.join(f"('{a}', '{b.replace(chr(39), chr(39)*2)}')" for a, b in batch)
    psql(f"UPDATE apartment_listings SET complex_name = v.cx FROM (VALUES {vals}) AS v(id, cx) WHERE apartment_listings.id = v.id")
print(f"привязано к ЖК по адресу: {zhk_linked}", flush=True)

# 3) сироты на фолбэке/без координат с номером дома -> Nominatim
geo = []
for l in psql(f"""
    SELECT id || chr(9) || COALESCE(address,'') || chr(9) || 'X'
    FROM apartment_listings WHERE complex_name IS NULL AND address ~ '[0-9]'
      AND (lat IS NULL OR round(lat::numeric,4) || ',' || round(lon::numeric,4) IN ({FALLBACK}))
""").splitlines():
    if not l:
        continue
    p = l.split('\t')
    geo.append((p[0], p[1]))
print(f"на геокодирование: {len(geo)}", flush=True)

UA = "hatuli-bot/1.0 (admin@hatuli.ai)"
ok = 0
batch = []
for i, (lid, addr) in enumerate(geo):
    clean = clean_addr(addr)
    got = None
    if clean and re.search(r'\d', clean):
        q = urllib.parse.quote("Астана, " + clean)
        got = None
        for attempt in range(3):
            try:
                req = urllib.request.Request(
                    f"https://nominatim.openstreetmap.org/search?q={q}&format=json&limit=1&countrycodes=kz",
                    headers={'User-Agent': UA})
                with urllib.request.urlopen(req, timeout=8) as r:
                    d = json.loads(r.read().decode())
                if d:
                    got = d[0]
                break
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(10 + attempt * 15)  # 10, 25, 40с — ждём бан
                    continue
                print(f"  ERR {addr[:40]}: HTTP {e.code}", flush=True)
                break
            except Exception as e:
                print(f"  ERR {addr[:40]}: {type(e).__name__} {str(e)[:50]}", flush=True)
                break
    if got:
        batch.append(f"('{lid}',{got['lat']},{got['lon']})")
        ok += 1
        if len(batch) >= 200:
            psql(f"UPDATE apartment_listings AS a SET lat=v.lat, lon=v.lon FROM (VALUES {','.join(batch)}) AS v(id,lat,lon) WHERE a.id=v.id")
            batch = []
    if (i + 1) % 25 == 0:
        print(f"  ...{i+1}/{len(geo)} ok={ok}", flush=True)
    time.sleep(1.1)
if batch:
    psql(f"UPDATE apartment_listings AS a SET lat=v.lat, lon=v.lon FROM (VALUES {','.join(batch)}) AS v(id,lat,lon) WHERE a.id=v.id")
print(f"геокодировано: {ok}/{len(geo)}", flush=True)

# итог
print(psql("SELECT COUNT(*) FILTER (WHERE lat IS NOT NULL) || '/' || COUNT(*) FROM apartment_listings WHERE complex_name IS NULL"), flush=True)
print(f"время: {round(time.time()-t0)}с", flush=True)
