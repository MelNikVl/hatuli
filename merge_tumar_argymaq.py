#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Объединение: Tumar Exclusive (канон 2891), Argymaq (канон 2431), восстановление Астана-Недвижимость (2756)."""
import subprocess

def psql(sql):
    r = subprocess.run(['sudo', '-u', 'postgres', 'psql', '-d', 'krisha_bot', '-t', '-A', '-c', sql],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:300])
    return r.stdout.strip()

# ── 1) TUMAR EXCLUSIVE: канон 2891 «Tumar Exclusive» ──
# все варианты имён -> 2891, все записи-копии -> garbage
TUMAR_EXCL = ['тумар exclusive', 'tumar exclusive', 'премиум класса tumar exclusive',
              'tumar exclusive от застройщика', 'жк tumar exclusive', 'tumar exclusive год постройки',
              'тумар эксклюзив', 'tumar — эксклюзивный дом бизнес']
TUMAR_EXCL_IDS = [2891, 2893, 2635, 2642, 1869, 2080, 2783, 1676, 3263]

for old in TUMAR_EXCL:
    psql(f"UPDATE apartment_listings SET complex_name='Tumar Exclusive' "
         f"WHERE lower(trim(complex_name)) = '{old}'")
psql("UPDATE complexes SET is_garbage=TRUE WHERE id IN (2893, 2635, 2642, 1869, 2080, 2783, 1676, 3263)")
psql("UPDATE complexes SET is_garbage=FALSE WHERE id=2891")
print("Tumar Exclusive: объединено в 2891, копии garbage")

# ── 2) ARGYMAQ: канон 2431 «Argymaq» ──
ARGYMAQ = ['argymaq', 'argymak', 'аргымак', 'argymaq от tumar group',
           'argymaq год постройки 2025 эта', 'argymaq год постройки 2026 эта',
           'argymaq — это современный жк ко', 'argymak дом комфорт класса с эл']
for old in ARGYMAQ:
    if old in ('argymaq', 'argymak', 'аргымак', 'argymaq от tumar group'):
        psql(f"UPDATE apartment_listings SET complex_name='Argymaq' "
             f"WHERE lower(trim(complex_name)) = '{old}'")
psql("UPDATE complexes SET is_garbage=TRUE WHERE id IN (2152, 2638, 3012, 1555, 2789, 2928, 3228)")
psql("UPDATE complexes SET is_garbage=FALSE WHERE id=2431")
print("Argymaq: объединено в 2431, копии garbage")

# ── 3) АСТАНА-НЕДВИЖИМОСТЬ: реальный ЖК на Крыше, вернуть из garbage ──
psql("UPDATE complexes SET is_garbage=FALSE WHERE id=2756")
print("Астана-Недвижимость: 2756 восстановлен (реальный ЖК с карточкой Крыши)")

# ── Проверка ──
print("\n=== ПРОВЕРКА ===")
print(psql("SELECT id, name, is_garbage FROM complexes WHERE id IN (2891, 2431, 2756)"))
print(psql("SELECT COUNT(*) || ' объявлений Tumar Exclusive' FROM apartment_listings WHERE lower(trim(complex_name))='tumar exclusive'"))
print(psql("SELECT COUNT(*) || ' объявлений Argymaq' FROM apartment_listings WHERE lower(trim(complex_name))='argymaq'"))
print(psql("SELECT COUNT(*) || ' объявлений Астана-Недвижимость' FROM apartment_listings WHERE lower(trim(complex_name))='астана-недвижимость'"))
print(psql("SELECT COUNT(*) || ' чистых ЖК' FROM complexes WHERE is_garbage IS NOT TRUE"))
