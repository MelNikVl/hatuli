#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Покрытие: все уникальные адреса vs house_years."""
import subprocess, re, sys
sys.path.insert(0, '/home/nik/krisha_bot')
from house_years_0 import norm_addr

def psql(sql):
    r = subprocess.run(['sudo', '-u', 'postgres', 'psql', '-d', 'krisha_bot', '-t', '-A',
                        '-F', chr(9), '-c', sql], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:300])
    return r.stdout.strip()

# все адреса
all_addrs = set()
for l in psql("SELECT COALESCE(address,'') || chr(9) || 'X' FROM apartment_listings WHERE address IS NOT NULL AND address != ''").splitlines():
    if not l:
        continue
    n = norm_addr(l.split('\t')[0])
    if len(n) >= 5:
        all_addrs.add(n)

# адреса с годом
with_year = set()
for l in psql("SELECT address || chr(9) || 'X' FROM house_years").splitlines():
    if l:
        with_year.add(l.split('\t')[0])

covered = all_addrs & with_year
missing = all_addrs - with_year

print(f"Всего уникальных адресов (нормализованных): {len(all_addrs)}")
print(f"  с годом постройки: {len(covered)} ({100.0*len(covered)/len(all_addrs):.1f}%)")
print(f"  БЕЗ года постройки: {len(missing)} ({100.0*len(missing)/len(all_addrs):.1f}%)")

# топ-15 улиц среди недостающих
from collections import Counter
streets = Counter()
for a in missing:
    m = re.match(r'(.*?)(\d[\d/а-я-]*)$', a)
    if m:
        streets[m.group(1).strip()] += 1
    else:
        streets[a] += 1
print("\nТоп-15 улиц среди адресов без года:")
for s, c in streets.most_common(15):
    print(f"  {c:4}  {s[:50]}")
