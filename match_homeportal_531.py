#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сопоставление остатка 531 ЖК (без карточки Крыши) с базой homeportal_objects.
По нормализованному имени (казахские буквы, префиксы жк/мжк, очереди)."""
import sys
sys.path.insert(0, "/home/nik/krisha_bot")
import re
from difflib import SequenceMatcher
import psycopg2

BASE = "/home/nik/krisha_bot"


def load_database_url() -> str:
    for line in open(f"{BASE}/.env", encoding="utf-8"):
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip()
    return "postgresql://krisha@localhost/krisha_bot"


_KAZ = str.maketrans({"ә": "а", "Ә": "А", "ұ": "у", "Ұ": "У", "ү": "у", "Ү": "У",
                      "қ": "к", "Қ": "К", "ң": "н", "Ң": "Н", "ө": "о", "Ө": "О",
                      "ғ": "г", "Ғ": "Г", "і": "и", "І": "И"})
_PREFIX = re.compile(r"^(жк|кг|кд|жилой комплекс|жилой массив|коттеджный городок|мкр|мжк|дом|квартал|зеленый квартал)\.?\s+", re.I)
_TAIL = re.compile(
    r"(город астана|г\.?\s*астана|астана|нур-султан|район[а-яё\s]*|"
    r"от застройщика|застройщик[а-яё\s]*|год постройки.*|этаж.*|комнат.*|"
    r"площа.*|квартир.*|сдач.*|заселен.*|ипотек.*|торга.*|"
    r"с элементами.*|в черновом.*|топ-локация.*|прекрасн.*|уютн.*|"
    r"отличн.*|хорош.*|шикарн.*|идеальн.*|акция.*|скидк.*|срочно.*|"
    r"под ключ.*|без комиссии.*|цена.*|по запросу.*|новый дом.*|ждет.*|"
    r"находит.*|расположен.*|создан.*|перспективн.*|самом центре.*|"
    r"многофункционал.*|продуктовые.*|детских садов.*|в ипотеку.*|"
    r"за наличный.*|перевести перевод.*|может быть.*|для проживания.*|"
    r"инвестиций.*|кластер.*|школа.*|ключи.*|остановка.*|ход.*автобус.*|"
    r"в одном из самых.*|собственным о.*|своим.*)", re.I)
_CLASS_TAIL = re.compile(r"\s+(комфорт|бизнес|премиум|элит|эконом)?\s*класса\s*$", re.I)
_ORD = re.compile(r"^(.+?)[\s\-_]*(\(?\d+\)?[\s\-_]*(очередь|этап|блок|секция)?|\(?\d+[а-я]?\)?|\([^)]*\))[\s\-_]*$", re.I)
_NUM = re.compile(r"[\s\-_]*(\(?\d+[а-я]?\)?|\(?\d+\)?)[\s\-_]*$", re.I)


def norm(name: str) -> str:
    n = (name or "").lower()
    n = n.translate(_KAZ)
    n = _PREFIX.sub("", n)
    m = _ORD.match(n)
    if m and len(m.group(1).strip()) >= 4:
        n = m.group(1).strip()
    n = _TAIL.sub("", n)
    n = _CLASS_TAIL.sub("", n)
    n = re.sub(r"[^a-zа-яё0-9 ]", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def base_no_num(name: str) -> str:
    n = norm(name)
    return _NUM.sub("", n).strip()


def fuzzy(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.92 if abs(len(a) - len(b)) <= 6 else 0.85
    return SequenceMatcher(None, a, b).ratio()


def main() -> None:
    conn = psycopg2.connect(load_database_url())
    cur = conn.cursor()

    cur.execute("""
        SELECT id, name FROM complexes
        WHERE is_garbage IS NOT TRUE AND krisha_url IS NULL
          AND NOT COALESCE(source_info ? 'korter',FALSE)
          AND NOT COALESCE(source_info ? 'homsters',FALSE)
          AND developer_id IS NULL
        ORDER BY id
    """)
    ours = cur.fetchall()
    print(f"Остаток ЖК: {len(ours)}", flush=True)

    cur.execute("SELECT object_id, name, matched_complex_id FROM homeportal_objects")
    hp = cur.fetchall()
    print(f"Homeportal объектов: {len(hp)} (сопоставлено: {sum(1 for r in hp if r[2])})", flush=True)

    # индексы homeportal по нормализованным именам
    hp_by_norm: dict[str, list] = {}
    hp_unmatched = [(oid, hname) for oid, hname, mid in hp]
    for oid, hname in hp_unmatched:
        nn = norm(hname)
        if len(nn) >= 3:
            hp_by_norm.setdefault(nn, []).append((oid, hname))

    sure, fuzzy_hits = [], []
    for cid, name in ours:
        n = norm(name)
        b = base_no_num(name)
        if len(n) < 3:
            continue
        # точное совпадение
        hit = None
        for key in (n, b):
            if key in hp_by_norm:
                hit = hp_by_norm[key][0]
                break
        if hit:
            sure.append((cid, name, hit[0], hit[1]))
            continue
        # фаззи >= 0.9
        best = None
        if len(n) >= 5:
            for nn2, items in hp_by_norm.items():
                s = max(fuzzy(n, nn2), fuzzy(b, nn2))
                if s >= 0.9 and (best is None or s > best[0]):
                    best = (s, items[0][0], items[0][1])
        if best:
            fuzzy_hits.append((cid, name, best[1], best[2], best[0]))

    print(f"\n=== ТОЧНЫЕ СОВПАДЕНИЯ: {len(sure)} ===")
    for cid, name, oid, hname in sorted(sure, key=lambda x: x[1]):
        print(f"  id={cid} {name:40s} -> HP {hname} (obj={oid})")

    print(f"\n=== ФАЗЗИ (>=0.9): {len(fuzzy_hits)} ===")
    for cid, name, oid, hname, s in sorted(fuzzy_hits, key=lambda x: -x[4]):
        print(f"  id={cid} {name:40s} [{s:.2f}] -> HP {hname} (obj={oid})")

    if "--apply" in sys.argv:
        upd = 0
        for cid, name, oid, hname in sure:
            cur.execute("UPDATE homeportal_objects SET matched_complex_id=%s, match_method='norm_name_601', matched_at=now() WHERE object_id=%s",
                        (cid, oid))
            upd += cur.rowcount
        conn.commit()
        print(f"\nПРИМЕНЕНО: сопоставлено {upd} объектов homeportal", flush=True)

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
