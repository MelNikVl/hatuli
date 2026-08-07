#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""1) Восстановить Елорда Даму по ул. Е16 (2769, реальный ЖК с карточкой Крыши).
2) «Вокзал Астана-1» (922) — локация (район ж/д вокзала), не ЖК: отвязать объявления."""
import subprocess

def psql(sql):
    r = subprocess.run(['sudo', '-u', 'postgres', 'psql', '-d', 'krisha_bot', '-t', '-A', '-c', sql],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:300])
    return r.stdout.strip()

# 1) Елорда Даму по ул. Е16 — восстановить (карточка Крыши живая: МЖК Елорда Даму по ул. Е16, Елорда Курылыс)
psql("UPDATE complexes SET is_garbage=FALSE WHERE id=2769")
print("Елорда Даму по ул. Е16 (2769): восстановлен")

# 2) Вокзал Астана-1 — не ЖК, а район ж/д вокзала: отвязать объявления (complex_name -> NULL)
psql("UPDATE apartment_listings SET complex_name=NULL WHERE lower(trim(complex_name))='вокзал астана-1'")
print("Вокзал Астана-1 (922): 24 объявления отвязаны (локация, не ЖК)")

print("\n=== ПРОВЕРКА ===")
print(psql("SELECT id, name, is_garbage FROM complexes WHERE id IN (2769, 922)"))
print(psql("SELECT COUNT(*) || ' объявлений Елорда Даму по ул. Е16' FROM apartment_listings WHERE lower(trim(complex_name))='елорда даму по ул. е16'"))
print(psql("SELECT COUNT(*) || ' объявлений с Вокзал Астана-1' FROM apartment_listings WHERE lower(trim(complex_name)) LIKE '%вокзал%'"))
print(psql("SELECT COUNT(*) || ' чистых ЖК' FROM complexes WHERE is_garbage IS NOT TRUE"))
