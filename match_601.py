#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
match_601.py v2 — сопоставление ЖК «только из объявлений» с каталогом Крыши
и нашими чистыми ЖК.

Правила:
- norm() срезает префиксы (жк/кг/кд/мжк/мкр/жилой комплекс...) и хвосты
  («город астана», «астана», «район …», «от застройщика», «год постройки…»);
- точное совпадение норм-имени с каталогом Крыши -> проставить krisha_url;
- точное совпадение с нашим чистым ЖК -> дубль (объединить);
- «Очередь»-варианты (N, -N, N-я очередь, блок X) -> привязываем к базовому;
- остальное -> в /tmp/match601_fuzzy.txt для ручного разбора.

Запуск: venv/bin/python match_601.py [--apply]
"""
from __future__ import annotations

import argparse
import re
import sys

import psycopg2

BASE = "/home/nik/krisha_bot"


def load_database_url() -> str:
    for line in open(f"{BASE}/.env", encoding="utf-8"):
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip()
    return "postgresql://krisha@localhost/krisha_bot"


_PREFIX = re.compile(r"^(жк|кг|кд|жилой комплекс|жилой массив|коттеджный городок|мкр|мжк|дом|квартал)\.?\s+", re.I)
# Хвосты-мусор (срезаем всё после маркера)
_TAIL = re.compile(
    r"(город астана|г\.?\s*астана|астана|нур-султан|район[а-яё\s]*|"
    r"от застройщика|застройщик[а-яё\s]*|год постройки.*|этаж.*|комнат.*|"
    r"площа.*|квартир.*|сдач.*|заселен.*|ипотек.*|торга.*|"
    r"с элементами.*|в черновом.*|топ-локация.*|прекрасн.*|уютн.*|"
    r"отличн.*|хорош.*|шикарн.*|идеальн.*|акция.*|скидк.*|срочно.*|"
    r"под ключ.*|без комиссии.*|цена.*|по запросу.*|новый дом.*|ждет.*|"
    r"находит.*|расположен.*|создан.*|перспективн.*|самом центре.*|"
    r"многофункционал.*|продуктовые.*|детских садов.*|в ипотеку.*|"
    r"за наличный.*)", re.I)
# «Класса» — срезаем только если это ПОСЛЕДНЕЕ слово («комфорт класса»),
# но не из середины («Премиум Класса Tumar Exclusive» = Tumar Exclusive!)
_CLASS_TAIL = re.compile(r"\s+(комфорт|бизнес|премиум|элит|эконом)?\s*класса\s*$", re.I)
_ORD = re.compile(r"^(.+?)[\s\-_]*(\(?\d+\)?[\s\-_]*(очередь|этап|блок|секция)?|\(?\d+[а-я]?\)?|\([^)]*\))[\s\-_]*$", re.I)


def norm(name: str) -> str:
    n = (name or "").lower()
    n = _PREFIX.sub("", n)
    # сначала порядковые хвосты (2, 3, 4, блок B...), потом мусорные
    m = _ORD.match(n)
    if m:
        base = m.group(1).strip()
        if len(base) >= 4:
            n = base
    n = _TAIL.sub("", n)
    n = _CLASS_TAIL.sub("", n)
    n = re.sub(r"[^a-zа-яё0-9 ]", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    conn = psycopg2.connect(load_database_url())
    cur = conn.cursor()

    cur.execute("""
        SELECT id, name FROM complexes
        WHERE is_garbage IS NOT TRUE AND krisha_url IS NULL
          AND NOT COALESCE(source_info ? 'korter', FALSE)
          AND NOT COALESCE(source_info ? 'homsters', FALSE)
          AND developer_id IS NULL
        ORDER BY id
    """)
    ours = cur.fetchall()
    print(f"ЖК «только из объявлений»: {len(ours)}", flush=True)

    cur.execute("SELECT name, url FROM krisha_complex_catalog")
    catalog = cur.fetchall()
    cat_by_norm: dict[str, list] = {}
    for cname, curl in catalog:
        cn = norm(cname)
        if len(cn) >= 3:
            cat_by_norm.setdefault(cn, []).append((cname, curl))
    print(f"Каталог Крыши: {len(catalog)} (ключей: {len(cat_by_norm)})", flush=True)

    cur.execute("""
        SELECT id, name FROM complexes
        WHERE is_garbage IS NOT TRUE AND krisha_url IS NOT NULL
        ORDER BY id
    """)
    clean = cur.fetchall()
    clean_by_norm: dict[str, int] = {}
    for cid, cname in clean:
        cn = norm(cname)
        if len(cn) >= 3 and cn not in clean_by_norm:
            clean_by_norm[cn] = cid

    sure_cat, sure_dup, fuzzy = [], [], []
    for cid, name in ours:
        n = norm(name)
        if len(n) < 3:
            fuzzy.append((cid, name, "имя слишком короткое", "", ""))
            continue

        # 1) точное совпадение с каталогом Крыши
        if n in cat_by_norm:
            cname, curl = cat_by_norm[n][0]
            sure_cat.append((cid, name, 1.0, cname, curl))
            continue
        # 2) точное совпадение с нашим чистым ЖК (дубль)
        if n in clean_by_norm:
            sure_dup.append((cid, name, 1.0, clean_by_norm[n], n))
            continue
        # 3) база по порядковому хвосту (без «2»/«блок B») — ищем базовое имя
        #    в каталоге и среди наших
        base = re.sub(r"^(.+?)[\s\-_]*(\(?\d+[а-я]?\)?|\(?\d+\)?[\s\-_]*очередь|блок\s*[a-zа-яё0-9]+)[\s\-_]*$", r"\1", n, flags=re.I)
        if base != n and len(base) >= 4:
            if base in cat_by_norm:
                cname, curl = cat_by_norm[base][0]
                sure_cat.append((cid, name, 0.95, cname, curl))
                continue
            if base in clean_by_norm:
                sure_dup.append((cid, name, 0.95, clean_by_norm[base], base))
                continue
        fuzzy.append((cid, name, "", "", ""))

    print(f"\nУверенные → карточка Крыши: {len(sure_cat)}", flush=True)
    for cid, name, s, cn, cu in sorted(sure_cat, key=lambda x: -x[2]):
        print(f"  {name:40s} [{s:.2f}] -> {cn} | {cu}", flush=True)

    print(f"\nУверенные → дубль нашего ЖК: {len(sure_dup)}", flush=True)
    for cid, name, s, did, dn in sorted(sure_dup, key=lambda x: -x[2]):
        print(f"  {name:40s} [{s:.2f}] -> ДУБЛЬ {dn} (id={did})", flush=True)

    print(f"\nНе сопоставлено (ручной разбор): {len(fuzzy)}", flush=True)
    with open("/tmp/match601_fuzzy.txt", "w", encoding="utf-8") as f:
        for cid, name, *rest in fuzzy:
            f.write(f"{cid}\t{name}\n")
    for cid, name, *rest in fuzzy[:60]:
        print(f"  id={cid} {name}", flush=True)
    print("Полный список: /tmp/match601_fuzzy.txt", flush=True)

    if args.apply:
        upd_cat = upd_dup = 0
        for cid, name, s, cn, cu in sure_cat:
            cur.execute("UPDATE complexes SET krisha_url = %s, updated_at = now() WHERE id = %s", (cu, cid))
            upd_cat += cur.rowcount
        for cid, name, s, did, dn in sure_dup:
            cur.execute("UPDATE apartment_listings SET complex_name = %s WHERE complex_name = %s", (dn, name))
            cur.execute("UPDATE complexes SET is_garbage = TRUE, updated_at = now() WHERE id = %s", (cid,))
            upd_dup += 1
        conn.commit()
        print(f"\nПРИМЕНЕНО: krisha_url: {upd_cat}, дублей объединено: {upd_dup}", flush=True)

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
