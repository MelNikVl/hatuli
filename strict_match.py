#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Строгий матчинг. Шаг 1: ОТКАЗ — отвязать все объявления, где complex_name не подтверждается
полным совпадением адреса (норм-адрес объявления == норм-адресу ЖК) или именем ЖК в объявлении.
Шаг 2: привязка сирот ТОЛЬКО по полному равенству норм-адреса или имени ЖК в тексте."""
import subprocess, re, sys, math
sys.path.insert(0, '/home/nik/krisha_bot')

def psql(sql):
    r = subprocess.run(['sudo', '-u', 'postgres', 'psql', '-d', 'krisha_bot', '-t', '-A',
                        '-F', chr(9), '-c', sql], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:300])
    return r.stdout.strip()

def norm_addr(a):
    a = (a or '').lower()
    a = re.sub(r'р-н[а-яё]*\s*', '', a)
    a = re.sub(r'[\u2014\u2013-].*$', '', a)
    a = re.sub(r'[^a-zа-я0-9 ]', ' ', a)
    a = re.sub(r'\s+', ' ', a).strip()
    return a

def norm_name(n):
    n = (n or '').lower()
    n = re.sub(r'^(жк|кг|кд|жилой комплекс|жилой массив|бигвилль|bigville)\s*', '', n)
    n = re.sub(r'[\u2014\u2013]', ' ', n)
    n = re.sub(r'[^a-zа-я0-9 ]', ' ', n)
    n = re.sub(r'\s+', ' ', n).strip()
    return n

# ── 1) все ЖК: id, name + полный набор их норм-адресов ──
zhk = {}
rows = psql("SELECT id, name FROM complexes WHERE is_garbage IS NOT TRUE").splitlines()
for r in rows:
    if not r:
        continue
    p = r.split('\t')
    if len(p) >= 2:
        zhk[int(p[0])] = {'name': p[1], 'nn': norm_name(p[1]), 'addrs': set()}

# адреса ЖК: все объявления ЖК -> норм-адреса
rows = psql("SELECT lower(trim(complex_name)), address FROM apartment_listings "
            "WHERE complex_name IS NOT NULL AND address IS NOT NULL AND length(address) > 3").splitlines()
for r in rows:
    if not r:
        continue
    p = r.split('\t')
    if len(p) < 2:
        continue
    na = norm_addr(p[1])
    if len(na) >= 6:
        for z in zhk.values():
            if z['nn'] and (z['nn'] == norm_name(p[0]) or norm_name(p[0]).startswith(z['nn'])):
                z['addrs'].add(na)
                break

# имя -> список ЖК-кандидатов (для быстрого поиска по имени)
by_nn = {}
for z in zhk.values():
    by_nn.setdefault(z['nn'], []).append(z)

print(f"ЖК: {len(zhk)}")

# ── 2) ОТКАЗ: объявления с complex_name, не подтверждённые адресом/именем ──
rows = psql("SELECT id, complex_name, address, title FROM apartment_listings "
            "WHERE complex_name IS NOT NULL").splitlines()
print(f"Объявлений с complex_name: {len(rows)}")
unbind = 0
for r in rows:
    if not r:
        continue
    p = r.split('\t')
    if len(p) < 4:
        continue
    lid, cname, addr, title = p[0], p[1], p[2], p[3]
    na = norm_addr(addr)
    nn_cur = norm_name(cname)
    cands = by_nn.get(nn_cur, [])
    ok = False
    for z in cands:
        if na and na in z['addrs']:
            ok = True
            break
        if z['nn'] and (nn_cur and z['nn'] in norm_name(addr) or z['nn'] in norm_name(title)):
            ok = True
            break
    if not ok:
        psql(f"UPDATE apartment_listings SET complex_name = NULL WHERE id::text = '{lid}'")
        unbind += 1

print(f"Отвязано (ложные привязки): {unbind}")

# ── 3) НОВЫЙ матчинг сирот: ТОЛЬКО полное равенство адреса ИЛИ имя ЖК в тексте ──
rows2 = psql("SELECT id, address, title FROM apartment_listings "
             "WHERE complex_name IS NULL AND address IS NOT NULL AND length(address) > 3").splitlines()
print(f"Сирот с адресом: {len(rows2)}")
bound = 0
for r in rows2:
    if not r:
        continue
    p = r.split('\t')
    if len(p) < 3:
        continue
    lid, addr, title = p[0], p[1], p[2]
    na = norm_addr(addr)
    nt = norm_name(addr + ' ' + title)
    best = None
    for z in zhk.values():
        if na and na in z['addrs']:
            best = z
            break
        if z['nn'] and len(z['nn']) >= 4 and z['nn'] in nt:
            best = z
            break
    if best:
        psql(f"UPDATE apartment_listings SET complex_name = '{best['name'].replace(chr(39), chr(39)*2)}' WHERE id::text = '{lid}'")
        bound += 1

print(f"Привязано (строго): {bound}")
print(psql("SELECT COUNT(*) || ' без ЖК' FROM apartment_listings WHERE complex_name IS NULL"))
