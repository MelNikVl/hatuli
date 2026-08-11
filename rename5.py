#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Переименования: Акерке 2, Восток, Jetisu Aspan, Green House, Shyraq."""
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
    print(f"  {old_name[:40]:40} -> {cname[:35]:35} (id={canon_id}, объявл.={n})")

# 1) 2472 -> Акерке 2 (переименование, канон свободен)
psql("UPDATE complexes SET name='Акерке 2' WHERE id=2472")
psql("UPDATE apartment_listings SET complex_name='Акерке 2' WHERE lower(trim(complex_name)) LIKE 'акерке 2%'")
print("  2472 Акерке 2 - Очень Теплый! -> Акерке 2 (переименован)")

# 2) Восток: 310 -> Восток (Бахус), 668 -> Восток
psql("UPDATE complexes SET name='Восток (Бахус)' WHERE id=310")
psql("UPDATE apartment_listings SET complex_name='Восток (Бахус)' WHERE lower(trim(complex_name))='восток'")
print("  310 Восток -> Восток (Бахус) [vostok-bahus]")
psql("UPDATE complexes SET name='Восток' WHERE id=668")
psql("UPDATE apartment_listings SET complex_name='Восток' WHERE lower(trim(complex_name))='восток (нажимеденова)'")
print("  668 Восток (Нажимеденова) -> Восток")
# 2225 — garbage дубль, удаляем
psql("DELETE FROM complexes WHERE id=2225")
print("  2225 (garbage-дубль) удалён")

# 3) 1150 Жетису Аспан -> Бигвилль Jetisu.Aspan (1741)
merge("Жетису Аспан Комфорт Класс От З", 1741)

# 4) 2345 Green House Premium -> green house premium (181)
merge("Green House Premium Год Постро", 181)

# 5) 3882 Shyraq - Идеальный Дом -> SHYRAQ (238)
merge("Shyraq - Идеальный Дом", 238)

print("\n=== ПРОВЕРКА ===")
print(psql("SELECT id || ' | ' || name || ' | g=' || is_garbage::text FROM complexes WHERE id IN (2472, 310, 668, 1741, 181, 238) ORDER BY id"))
print(psql("SELECT COUNT(*) || ' чистых' FROM complexes WHERE is_garbage IS NOT TRUE"))
