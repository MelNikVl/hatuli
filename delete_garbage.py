#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Удаление всех мусорных записей complexes (is_garbage=TRUE):
1) объявления мусорных ЖК -> канон (если есть не-garbage тёзка по norm_name или тот же krisha_url)
2) иначе complex_name -> NULL (объявления остаются, без привязки к ЖК)
3) DELETE мусорных из complexes"""
import subprocess, sys
sys.path.insert(0, '/home/nik/krisha_bot')
from complexes_cleanup import norm_name

def psql(sql):
    r = subprocess.run(['sudo', '-u', 'postgres', 'psql', '-d', 'krisha_bot', '-t', '-A', '-c', sql],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:300])
    return r.stdout.strip()

# все ЖК: id, name, url, garbage
rows = [l.split('\t') for l in psql(
    "SELECT id || chr(9) || name || chr(9) || COALESCE(krisha_url,'') || chr(9) || is_garbage::text "
    "FROM complexes").splitlines() if l]

live = {}        # norm_name -> (id, name) живой
url2live = {}    # krisha_url -> (id, name) живой
garbage = []     # (id, name, url)
for cid, name, url, g in rows:
    if g == 'true':
        garbage.append((int(cid), name, url))
    else:
        nn = norm_name(name)
        if nn and nn not in live:
            live[nn] = (int(cid), name)
        if url and url not in url2live:
            url2live[url] = (int(cid), name)

print(f"Мусорных: {len(garbage)}")

rebind = {}   # старое имя -> имя канона
for cid, name, url in garbage:
    nn = norm_name(name)
    canon = live.get(nn)
    if canon is None and url:
        canon = url2live.get(url)
    if canon is None:
        for lnn, (lid, lname) in live.items():
            if nn and lnn and len(nn) >= 5 and len(lnn) >= 5 and (nn in lnn or lnn in nn):
                canon = (lid, lname)
                break
    if canon is not None:
        rebind[name] = canon[1]

print(f"Будет перепривязано к канонам: {len(rebind)}")
print(f"Будет отвязано (complex_name=NULL): {len(garbage) - len(rebind)}")

# применяем: перепривязка
n_rebound = 0
for old, canon_name in rebind.items():
    psql(f"UPDATE apartment_listings SET complex_name = '{canon_name.replace(chr(39), chr(39)*2)}' "
         f"WHERE lower(trim(complex_name)) = '{old.lower().replace(chr(39), chr(39)*2)}'")
    n_rebound += 1

# отвязать остальные (нет канона)
for cid, name, url in garbage:
    if name not in rebind:
        psql(f"UPDATE apartment_listings SET complex_name=NULL "
             f"WHERE lower(trim(complex_name)) = '{name.lower().replace(chr(39), chr(39)*2)}'")

# удалить мусорные
ids = ",".join(str(c) for c, _, _ in garbage)
psql(f"DELETE FROM complexes WHERE id IN ({ids})")
print(f"\nУдалено записей: {len(garbage)}, перепривязано объявлений: {n_rebound}")
print(psql("SELECT COUNT(*) || ' ЖК осталось' FROM complexes"))
print(psql("SELECT is_garbage, COUNT(*) FROM complexes GROUP BY 1"))
