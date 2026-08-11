#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Фикс ложных is_street: реальные ЖК (есть на Крыше) — вернуть, мусор оставить."""
import subprocess

def psql(sql):
    r = subprocess.run(['sudo', '-u', 'postgres', 'psql', '-d', 'krisha_bot', '-t', '-A', '-c', sql],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:300])
    return r.stdout.strip()

# Реальные ЖК (подтверждены карточками Крыши) — снять is_street + проставить url
FIX = {
    2614: "https://krisha.kz/complex/show/astana/saryarka/",   # Сарыарка
    2933: "https://krisha.kz/complex/show/astana/esil/",        # Есиль
    2402: "https://krisha.kz/complex/show/astana/batyr/",       # Батыр
    749:  "https://krisha.kz/complex/show/astana/leja/",        # Лея
    1121: "https://krisha.kz/complex/show/astana/dostyk/",      # Достык (КазСтрой)
    1609: "https://krisha.kz/complex/show/astana/turan/",       # Туран (Востокстрой)
    2360: "https://krisha.kz/complex/show/nur-sultan/akansery/",  # Акан Серы (Mabex Trade)
}
for cid, url in FIX.items():
    n = psql(f"UPDATE complexes SET is_street=FALSE, krisha_url='{url}', updated_at=now() WHERE id={cid}")
    print(f"  {cid}: is_street снят, krisha_url={url}")

# Проверка: у кого из снятых есть координаты? (аудит мог обнулить)
for cid in FIX:
    lat = psql(f"SELECT COALESCE(lat::text, 'NULL') FROM complexes WHERE id={cid}")
    print(f"  {cid} lat={lat}")

# Мусор остаётся is_street: Бараева (улица), Тан (разные улицы), Астана (город),
# Туран 1609 — стоп, Туран реальный ЖК. Проверяю Бараеву/Тан/Астана:
print("\nОстались улицами:", psql("SELECT id || ':' || name FROM complexes WHERE is_street=TRUE AND (name IN ('Бараева','Тан','Астана','Толе Би','Коргалжын','Байтурсын','Уркер','Аксу','Байконур','Алматы','Сауран','Уют','7Я','Айым','Тасты','Кордай','Роза Баглановой','Даулеткерей','ЖК Жана') OR id IN (2873,3050,3008,3043,2720,3634,3954,2650,2114,3574,3591,2562,3514,322,1879,3029,3665))"))
