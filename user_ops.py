#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Все операции пользователя: удаление/объединение/переименование/описания."""
import subprocess

def psql(sql):
    r = subprocess.run(['sudo', '-u', 'postgres', 'psql', '-d', 'krisha_bot', '-t', '-A', '-c', sql],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:300])
    return r.stdout.strip()

def merge(old_name, canon_id):
    cname = psql(f"SELECT name FROM complexes WHERE id={canon_id}").splitlines()[0]
    old = old_name.lower().strip().replace("'", "''")
    n = psql(f"SELECT COUNT(*) FROM apartment_listings WHERE lower(trim(complex_name)) = '{old}'")
    psql(f"UPDATE apartment_listings SET complex_name = '{cname.replace(chr(39), chr(39)*2)}' "
         f"WHERE lower(trim(complex_name)) = '{old}'")
    cid = psql(f"SELECT id FROM complexes WHERE lower(trim(name)) = '{old}' LIMIT 1")
    if cid:
        psql(f"UPDATE complexes SET is_garbage=TRUE WHERE id = {cid.splitlines()[0]}")
    print(f"  {old_name[:38]:38} -> {cname[:32]:32} (id={canon_id}, объявл.={n})")

print("=== 1) 2565 -> Бейбарыс (дубль, Нажмеденова 34/2) ===")
merge("Есть Образовательные Центры", 1336)

print("=== 2) 2429 — старые дома, не ЖК: отвязать и удалить ===")
n = psql("SELECT COUNT(*) FROM apartment_listings WHERE lower(trim(complex_name)) = 'расположен в центре правого бер'")
psql("UPDATE apartment_listings SET complex_name=NULL WHERE lower(trim(complex_name)) = 'расположен в центре правого бер'")
psql("DELETE FROM complexes WHERE id=2429")
print(f"  отвязано {n}, запись удалена (объявления остаются на карте по адресам)")

print("=== 3) 1333 -> ainabulaq (дубль, Мәңгілік ел × Хусейн) ===")
merge("Расположен На Левом Берегу", 122)

print("=== 4) 2533 -> Dariya от NAK (Сыганак 12) ===")
merge("Dariya - Это Современный Проект", 169)

print("=== 5) 2765 -> Sultan Beibarys (Сыганак 22/1) ===")
merge("Sultan Beibarys От Надёжного За", 1045)

print("=== 6) 666 -> Вдоль ручья Сарыбулак (дубль 2807) ===")
merge("Вдоль Ручья Сарыбулак По Ул", 2807)

print("=== 7) 407 -> nova city (Аль-Фараби 34/2 = Nova City 4) ===")
merge("Несколько Детских Садов", 213)

print("=== 8) 2220 -> Азат (Куйши Дина 30/1) ===")
merge("Продается Помещение Площадью 46", 2228)

print("=== 9) 2589 -> safar (Байтурсынова 36 = Safar 2 Irtysh) ===")
merge("Расположен Возле Будущей Аллеи", 223)

print("=== 10) 3808 -> Tumar Exclusive (Токпанова 18) ===")
merge("Премиум Класса", 2891)

print("\n=== ПРОВЕРКА ===")
print(psql("SELECT COUNT(*) || ' чистых ЖК' FROM complexes WHERE is_garbage IS NOT TRUE"))
print(psql("SELECT COUNT(*) || ' без ЖК' FROM apartment_listings WHERE complex_name IS NULL"))
