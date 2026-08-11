#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Чистка 20 мусорных ЖК: объединение с канонами, переименование, отвязка."""
import subprocess

def psql(sql):
    r = subprocess.run(['sudo', '-u', 'postgres', 'psql', '-d', 'krisha_bot', '-t', '-A', '-c', sql],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:300])
    return r.stdout.strip()

def merge(old_name, canon_id):
    """Объявления старого имени -> имя канона; запись -> garbage."""
    cname = psql(f"SELECT name FROM complexes WHERE id={canon_id}")
    if not cname:
        print(f"  !! канон {canon_id} не найден")
        return
    cname = cname.splitlines()[0]
    old = old_name.lower().strip().replace("'", "''")
    n = psql(f"SELECT COUNT(*) FROM apartment_listings WHERE lower(trim(complex_name)) = '{old}'")
    psql(f"UPDATE apartment_listings SET complex_name = '{cname.replace(chr(39), chr(39)*2)}' "
         f"WHERE lower(trim(complex_name)) = '{old}'")
    print(f"  {old_name[:40]:40} -> {cname[:35]:35} (id={canon_id}, объявл.={n})")

def rename(old_name, new_name):
    old = old_name.lower().strip().replace("'", "''")
    n = psql(f"SELECT COUNT(*) FROM apartment_listings WHERE lower(trim(complex_name)) = '{old}'")
    psql(f"UPDATE apartment_listings SET complex_name = '{new_name.replace(chr(39), chr(39)*2)}' "
         f"WHERE lower(trim(complex_name)) = '{old}'")
    psql(f"UPDATE complexes SET name = '{new_name.replace(chr(39), chr(39)*2)}' WHERE name ILIKE '{old_name[:30]}%'")
    print(f"  {old_name[:40]:40} -> {new_name[:35]:35} (переименован, объявл.={n})")

def detach(name):
    old = name.lower().strip().replace("'", "''")
    n = psql(f"SELECT COUNT(*) FROM apartment_listings WHERE lower(trim(complex_name)) = '{old}'")
    psql(f"UPDATE apartment_listings SET complex_name = NULL WHERE lower(trim(complex_name)) = '{old}'")
    cid = psql(f"SELECT id FROM complexes WHERE lower(trim(name)) = '{old}' LIMIT 1")
    if cid:
        psql(f"DELETE FROM complexes WHERE id = {cid.splitlines()[0]}")
    print(f"  {name[:40]:40} -> отвязано ({n} объявл. осталось на карте по координатам)")

print("=== ОБЪЕДИНЕНИЕ С КАНОНАМИ ===")
merge("Baimura2 Район Ханшатыра", 1006)      # BAIMURA (51.1263/71.3762, url=baimura)
merge("New City Life Астана Район Нура", 211) # New City Life
merge("Sandi Qala 2 • Район Sfera Park", 891) # Sandi Qala 2
merge("Turan House Год Постройки 2024", 3530) # Туран Хаус
merge("Ару-Парк-3 В Районе Коттеджного", 142) # arupark
merge("Бирлик На 3 Этаже Из 5", 2809)         # Бирлик (51.1956/71.3898)
merge("Гаухартас2 На 6 Этаже", 3236)          # Gauhartas 2
merge("Кернай 2022 Года Постройки", 727)      # Кернай
merge("Саранда 3 Этаж", 438)                  # Саранда (51.1010/71.4539)
merge("Находится В Перспективном Район", 1006) # BAIMURA (адрес Е-15 16 — район Баймура)
merge("Расположен В Отличном Районе Ас", 668)  # Восток (Нажимеденова) (адрес Нажимеденов 17)
merge("Триумф Сити В Районе Триумфальн", 242)  # triumph city (51.0984/71.4336)
# бонус: дубль 469 «Триумф Сити» -> 242 (тот же ЖК)
merge("Триумф Сити", 242)

print("\n=== ПЕРЕИМЕНОВАНИЕ (без канона) ===")
rename("Hayat Год Постройки 2020 Этаж", "Hayat")
rename("Sati Club House Год Постройки", "Sati Club House")

print("\n=== ОТВЯЗКА (чистый мусор, ЖК удаляется, объявления остаются) ===")
detach("В Районе")
detach("В Хорошем Районе Экологически")
detach("На 10 Этаже")
detach("Расположен В Районе С Развитой")
detach("Состоит Из 13 Одинаковых Этажны")
detach("Расположен В Перспективном Район")  # бонус (аналог 3372, если есть)

print("\n=== ПРОВЕРКА ===")
print(psql("SELECT COUNT(*) || ' чистых ЖК' FROM complexes WHERE is_garbage IS NOT TRUE"))
print(psql("SELECT COUNT(*) || ' всего' FROM complexes"))
