#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Повторная геокодировка notfound: без района, чистый адрес. Пауза 1.1с, checkpoint."""
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
    x = re.sub(r'\b(р-н|район|р н)\b\s*,?\s*', '', x, flags=re.I)  # убрать район
    x = re.sub(r'\b(ул\.?|улица|пр\.?|проспект|пер\.?|переулок)\s*', '', x, flags=re.I)
    x = re.sub(r'[«»"\']', '', x)
    x = re.sub(r'\s+', ' ', x).strip()
    return x

# notfound id из старого checkpoint
nf_ids = set()
for l in open('/tmp/geo_cp.txt', encoding='utf-8'):
    p = l.rstrip('\n').split('\t')
    if len(p) >= 4 and p[3] == 'notfound':
        nf_ids.add(p[0])
print(f"notfound для повторной попытки: {len(nf_ids)}", flush=True)

# уже повторно обработанные
CP2 = '/tmp/geo_cp2.txt'
done2 = set()
if os.path.exists(CP2):
    for l in open(CP2, encoding='utf-8'):
        p = l.rstrip('\n').split('\t')
        if p:
            done2.add(p[0])
print(f"повторно уже обработано: {len(done2)}", flush=True)

todo_ids = nf_ids - done2
print(f"к обработке: {len(todo_ids)}", flush=True)

if not todo_ids:
    print("все уже обработаны", flush=True)
    sys.exit(0)

# адреса
addr_map = {}
for l in psql(f"""
    SELECT id || chr(9) || COALESCE(address,'') || chr(9) || 'X'
    FROM apartment_listings WHERE id IN ({','.join("'" + i.replace("'", "''") + "'" for i in list(todo_ids))})
""").splitlines():
    if not l:
        continue
    p = l.split('\t')
    addr_map[p[0]] = p[1]

UA = "hatuli-bot/1.0 (admin@hatuli.ai)"
ok = 0
with open(CP2, 'a', encoding='utf-8') as f:
    for i, (lid, addr) in enumerate(addr_map.items()):
        clean = clean_addr(addr)
        got = None
        if clean and re.search(r'\d', clean):  # только с номером
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
            f.write(f"{lid}\t{got['lat']}\t{got['lon']}\tnominatim2\t\n")
            ok += 1
        else:
            f.write(f"{lid}\t\t\tnotfound2\t\n")
        f.flush()
        if (i + 1) % 25 == 0:
            print(f"  ...{i+1}/{len(addr_map)} ok={ok}", flush=True)
        time.sleep(1.1)
print(f"повторно геокодировано: {ok}/{len(addr_map)}", flush=True)

# запись координат
batch = []
for l in open(CP2, encoding='utf-8'):
    p = l.rstrip('\n').split('\t')
    if len(p) >= 4 and p[3] == 'nominatim2' and p[1]:
        batch.append(f"('{p[0].replace(chr(39), chr(39)*2)}',{p[1]},{p[2]})")
        if len(batch) >= 300:
            psql(f"UPDATE apartment_listings AS a SET lat=v.lat, lon=v.lon FROM (VALUES {','.join(batch)}) AS v(id,lat,lon) WHERE a.id=v.id")
            batch = []
if batch:
    psql(f"UPDATE apartment_listings AS a SET lat=v.lat, lon=v.lon FROM (VALUES {','.join(batch)}) AS v(id,lat,lon) WHERE a.id=v.id")
print("координаты записаны", flush=True)
print(psql("SELECT COUNT(*) FILTER (WHERE lat IS NOT NULL) || '/' || COUNT(*) FROM apartment_listings WHERE complex_name IS NULL"), flush=True)
