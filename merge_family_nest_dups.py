#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Слияние дублей Family Nest (задача 2026-08-12, найдено фиксом буквенных
блоков в _phase_token() — 'Family Nest F' vs 'Family Nest' дало 0.81 auto,
разбор соседей вскрыл дубли строк complexes). Тот же шаблон, что
merge_dups2.py: канон = больше apartment_listings, дубль -> is_garbage,
объявления перепривязаны по complex_name (текстовое поле, не FK).

Слито здесь только то, что однозначно один и тот же объект:
  3235 'Family Nest - F Блок' (2 объявл., 0 units/links)
    -> 2481 'Family Nest F' (11 объявл., 0 units/links) — оба явно про
       блок F, координаты 238 м друг от друга (геокодинг неточный, не
       разные объекты).
  172 'Бигвилль Family Nest' (26 объявл., 1 source_link krisha)
    -> 3743 'Family Nest' (10 объявл., 340 units, 1 source_link bi_group) —
       системный паттерн 'Бигвилль X' = 'X', встречался весь этот сеанс
       (GreenLine.Velora, Jetisu.Aspan, ...). source_link у 172 (krisha)
       переносится на 3743, у которого своего krisha-линка ещё нет.

НЕ трогает 3996 'Family Nest F | Bi Group' — это НЕ дубль этих двух, а
отдельная, более серьёзная находка: туда через СТАРЫЙ (до фикса токена
фазы) матчинг слиплись 3 РАЗНЫХ homeportal-объекта — 'Family Nest A'
(1338), 'Family Nest B' (1339), 'Family Nest F' (1357), по разным
адресам (Е 354 уч.3 vs пр. Әл-Фараби уч. 73А). Это надо расшивать
отдельно (создавать/находить верные complex_id под A/B/F и разносить
by object_id), не сюда, не автоматически.
"""
import subprocess


def psql(sql: str) -> str:
    r = subprocess.run(['sudo', '-u', 'postgres', 'psql', '-d', 'krisha_bot', '-t', '-A', '-c', sql],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:300])
    return r.stdout.strip()


def esc(s: str) -> str:
    return s.replace("'", "''")


MERGES = [
    (3235, 2481),  # Family Nest - F Блок -> Family Nest F
    (172, 3743),   # Бигвилль Family Nest -> Family Nest
]

for dup_id, canon_id in MERGES:
    names = {}
    for line in psql(f"SELECT id, name FROM complexes WHERE id IN ({dup_id}, {canon_id})").splitlines():
        cid, name = line.split("|", 1)
        names[int(cid)] = name
    dup_name, canon_name = names[dup_id], names[canon_id]

    n_listings = psql(
        f"SELECT COUNT(*) FROM apartment_listings WHERE lower(trim(complex_name)) = '{esc(dup_name.lower().strip())}'")
    psql(f"UPDATE apartment_listings SET complex_name = '{esc(canon_name)}' "
         f"WHERE lower(trim(complex_name)) = '{esc(dup_name.lower().strip())}'")

    # source_links дубля -> канон, только если у канона такого (source,
    # source_id) ещё нет (UNIQUE), иначе оставляем дублю (редкий кейс,
    # не теряем сигнал молча — просто не трогаем эту конкретную строку).
    links = psql(f"SELECT source, source_id FROM complex_source_links WHERE complex_id = {dup_id}").splitlines()
    moved_links = 0
    for line in links:
        if not line:
            continue
        source, source_id = line.split("|", 1)
        exists = psql(
            f"SELECT 1 FROM complex_source_links WHERE source='{esc(source)}' AND source_id='{esc(source_id)}' AND complex_id != {dup_id}")
        if exists:
            print(f"  ! source_link {source}/{source_id} уже есть у другого complex_id — не трогаю")
            continue
        psql(f"UPDATE complex_source_links SET complex_id = {canon_id} "
             f"WHERE source = '{esc(source)}' AND source_id = '{esc(source_id)}'")
        moved_links += 1

    psql(f"UPDATE complexes SET is_garbage = TRUE WHERE id = {dup_id}")
    print(f"{dup_id} {dup_name!r} -> {canon_id} {canon_name!r}: "
          f"объявлений={n_listings}, source_links перенесено={moved_links}")

print()
print("Проверка:")
print(psql(f"SELECT c.id, c.name, c.is_garbage, "
           f"(SELECT count(*) FROM apartment_listings a WHERE lower(trim(a.complex_name))=lower(trim(c.name))) AS listings "
           f"FROM complexes c WHERE c.id IN (172,2481,3235,3743) ORDER BY c.id"))
