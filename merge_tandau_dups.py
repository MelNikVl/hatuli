#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Слияние транслит-дубля Tandau (задача 2026-08-12, гейт 2, п.5 —
транслит-сигнал): id=2217 'Tandau' (латиница) и id=2563 'Тандау'
(кириллица) — то же самое имя в двух алфавитах, координаты 51.1220 vs
51.1218 (~25 м) — тот же дом, не разные ЖК. pg_trgm без транслитерации
их не видит похожими (буквы разные), поэтому дубль и не поймался
score_match() раньше.

id=2315 'Comfort Tandau' — НЕ трогаем: 'Comfort' продуктовый токен
(Highvill-пенальти, не транслит), координаты 51.0801/71.3821 — это уже
несколько км от 2217/2563, реально другой адрес/объект, совпадение
имени случайно.

Канон = больше данных (тот же критерий, что merge_family_nest_dups.py):
2217 (14 объявл., 2 source_links, 1 tech_specs) против 2563
(1 объявл., 0/0) — канон 2217, дубль 2563 -> is_garbage.
"""
import subprocess

DUP_ID, CANON_ID = 2563, 2217


def psql(sql: str) -> str:
    r = subprocess.run(['sudo', '-u', 'postgres', 'psql', '-d', 'krisha_bot', '-t', '-A', '-c', sql],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:300])
    return r.stdout.strip()


def esc(s: str) -> str:
    return s.replace("'", "''")


names = {}
for line in psql(f"SELECT id, name FROM complexes WHERE id IN ({DUP_ID}, {CANON_ID})").splitlines():
    cid, name = line.split("|", 1)
    names[int(cid)] = name
dup_name, canon_name = names[DUP_ID], names[CANON_ID]

n_listings = psql(
    f"SELECT COUNT(*) FROM apartment_listings WHERE lower(trim(complex_name)) = '{esc(dup_name.lower().strip())}'")
psql(f"UPDATE apartment_listings SET complex_name = '{esc(canon_name)}' "
     f"WHERE lower(trim(complex_name)) = '{esc(dup_name.lower().strip())}'")

links = psql(f"SELECT source, source_id FROM complex_source_links WHERE complex_id = {DUP_ID}").splitlines()
moved_links = 0
for line in links:
    if not line:
        continue
    source, source_id = line.split("|", 1)
    exists = psql(
        f"SELECT 1 FROM complex_source_links WHERE source='{esc(source)}' AND source_id='{esc(source_id)}' AND complex_id != {DUP_ID}")
    if exists:
        print(f"  ! source_link {source}/{source_id} уже есть у другого complex_id — не трогаю")
        continue
    psql(f"UPDATE complex_source_links SET complex_id = {CANON_ID} "
         f"WHERE source = '{esc(source)}' AND source_id = '{esc(source_id)}'")
    moved_links += 1

psql(f"UPDATE complexes SET is_garbage = TRUE WHERE id = {DUP_ID}")
print(f"{DUP_ID} {dup_name!r} -> {CANON_ID} {canon_name!r}: "
      f"объявлений перенесено={n_listings}, source_links перенесено={moved_links}")

print()
print("Проверка:")
print(psql(f"SELECT c.id, c.name, c.is_garbage, "
           f"(SELECT count(*) FROM apartment_listings a WHERE lower(trim(a.complex_name))=lower(trim(c.name))) AS listings "
           f"FROM complexes c WHERE c.id IN ({DUP_ID},{CANON_ID}) ORDER BY c.id"))
