#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
complexes_cleanup.py — чистка дублей/мусора в complexes (повторный прогон 2026-08-04).

Схема:
1. norm_name(): lower, срезать «жк», всё после |/—/(, «год постройки…»,
   эмодзи/₸/цены/адреса ([^a-zа-яё0-9 ] → пробел, схлопнуть).
2. Группировка по норм. имени среди НЕ-garbage строк; каноническая строка =
   имя == норм. имени (чистая), +бонус за krisha_url и фото, иначе самая короткая.
3. НЕ удаляем: is_garbage = TRUE + перепривязка объявлений
   UPDATE apartment_listings SET complex_name = каноническое
   WHERE complex_name = мусорное (объявления ссылаются на имя ТЕКСТОМ, не FK!).
4. Одиночный мусор (норм. ≠ имени + маркеры JUNK) тоже помечается.
   Уже-помеченные garbage НЕ трогаем (идемпотентно).

Запуск: venv/bin/python complexes_cleanup.py [--apply]
"""
from __future__ import annotations

import argparse
import re

import psycopg2

BASE = "/home/nik/krisha_bot"


def load_database_url() -> str:
    for line in open(f"{BASE}/.env", encoding="utf-8"):
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip()
    return "postgresql://krisha@localhost/krisha_bot"


def norm_name(name: str) -> str:
    n = (name or "").lower()
    n = re.sub(r"^(жк|кг|жилой комплекс|жилой массив|коттеджный городок|мкр)\.?\s+", "", n)
    n = re.split(r"[\||—|–|(]", n)[0]
    n = re.sub(r"год постройки.*", "", n)
    n = re.sub(r"[^a-zа-яё0-9 ]", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


JUNK_MARKERS = [
    "от застройщик", "застройщик", "по запросу", "под ключ", "без комиссии",
    "срочно", "уютн", "хорош", "шикарн", "идеальн", "прекрасн", "отличн",
    "акция", "скидк", "торга", "цена", "расположен", "находит", "находятся",
    "создан в концепции", "в черновом", "ждет своего", "новый дом",
    "квартира", "места!", "класса", "комфорт+", "комфорт плюс", "элементами",
    "перспективн", "золотой середине", "самом центре", "многофункционал",
    "продуктовые магазины", "детских садов", "новостройк", "астана",
    "город астана", "нур-султан", "в ипотеку", "за наличный",
]
JUNK_RE = re.compile("|".join(re.escape(m) for m in JUNK_MARKERS))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="реально применить (без него — dry-run)")
    args = ap.parse_args()

    conn = psycopg2.connect(load_database_url())
    conn.autocommit = False
    cur = conn.cursor()

    cur.execute("""
        SELECT id, name, krisha_url, photos, is_garbage
        FROM complexes ORDER BY id
    """)
    rows = cur.fetchall()

    # 1) Группировка по норм. имени — только НЕ-garbage строки
    groups: dict[str, list[dict]] = {}
    for cid, name, krisha_url, photos, is_garbage in rows:
        if is_garbage:
            continue
        nn = norm_name(name)
        if not nn:
            continue
        groups.setdefault(nn, []).append({
            "id": cid, "name": name, "nn": nn,
            "has_url": bool(krisha_url),
            "has_photos": bool(photos and photos != "[]" and photos != "null"),
        })

    def pick_canonical(group: list[dict]) -> dict:
        clean = [g for g in group if g["name"].lower() == g["nn"]]
        cands = clean if clean else group
        cands = sorted(cands, key=lambda g: (g["has_url"], g["has_photos"], -len(g["name"])), reverse=True)
        return cands[0]

    garbage_ids: list[int] = []
    rebind: dict[str, str] = {}
    single_junk: list[int] = []
    groups_found = 0

    for nn, group in groups.items():
        if len(group) > 1:
            canon = pick_canonical(group)
            groups_found += 1
            for g in group:
                if g["id"] == canon["id"]:
                    continue
                garbage_ids.append(g["id"])
                rebind[g["name"]] = canon["name"]
        else:
            g = group[0]
            if g["name"].lower() != g["nn"] and JUNK_RE.search(g["name"].lower()):
                single_junk.append(g["id"])

    n_garbage = len(garbage_ids) + len(single_junk)
    print(f"Групп-дублей: {groups_found}, строк в мусор: {len(garbage_ids)}, "
          f"одиночных мусорных: {len(single_junk)}, итого к пометке: {n_garbage}", flush=True)

    if args.apply:
        if garbage_ids:
            cur.execute("UPDATE complexes SET is_garbage = TRUE, updated_at = now() "
                        "WHERE id = ANY(%s)", (garbage_ids,))
        if single_junk:
            cur.execute("UPDATE complexes SET is_garbage = TRUE, updated_at = now() "
                        "WHERE id = ANY(%s)", (single_junk,))
        rebound = 0
        for old_name, new_name in rebind.items():
            cur.execute("UPDATE apartment_listings SET complex_name = %s "
                        "WHERE complex_name = %s", (new_name, old_name))
            rebound += cur.rowcount
        conn.commit()
        cur.execute("SELECT COUNT(*) FROM complexes WHERE is_garbage IS NOT TRUE")
        clean_total = cur.fetchone()[0]
        print(f"ПРИМЕНЕНО. Перепривязано объявлений: {rebound}. Чистых ЖК осталось: {clean_total}", flush=True)
        with open("/tmp/cleanup_20260804.txt", "w", encoding="utf-8") as f:
            for old_name, new_name in sorted(rebind.items()):
                f.write(f"{old_name}\t->\t{new_name}\n")
        print("Аудит: /tmp/cleanup_20260804.txt", flush=True)
    else:
        conn.rollback()
        print("DRY-RUN: ничего не изменено. Запусти с --apply для применения.", flush=True)
        shown = 0
        for nn, group in sorted(groups.items()):
            if len(group) > 1 and shown < 15:
                canon = pick_canonical(group)
                m = ", ".join(f"{g['name']}" for g in group if g['id'] != canon['id'])
                print(f"  [{nn}] канон={canon['name']} -> {m[:140]}")
                shown += 1

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
