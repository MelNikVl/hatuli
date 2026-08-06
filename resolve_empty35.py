#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Разбор пустых ЖК — v2: казахская нормализация (ә->а, ұ->у, қ->к...)
+ фаззи-поиск канона среди наших ЖК и каталога Крыши."""
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
_PREFIX = re.compile(r"^(жк|кг|кд|жилой комплекс|жилой массив|коттеджный городок|мкр|мжк|дом|квартал)\.?\s+", re.I)
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
    n = _NUM.sub("", n).strip()
    return n


def fuzzy(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.92 if abs(len(a) - len(b)) <= 6 else 0.85
    return SequenceMatcher(None, a, b).ratio()


JUNK_MARKERS = [
    "перевести", "может быть", "для проживания", "инвестиций", "кластер",
    "школа", "ключи", "остановка", "автобус", "в одном из самых", "собственным",
    "перевод", "город астана", "от застройщика", "надёжного", "класса",
    "расположен", "престижном", "в самом сердце", "уютн", "отличн", "хорош",
    "новый дом", "стиль", "своим",
]


def main() -> None:
    conn = psycopg2.connect(load_database_url())
    cur = conn.cursor()

    cur.execute("SELECT id, name, krisha_url FROM complexes WHERE is_garbage IS NOT TRUE AND krisha_url IS NOT NULL")
    clean = cur.fetchall()
    clean_norms = [(cid, cn, norm(cn)) for cid, cn, _ in clean if len(norm(cn)) >= 3]

    cur.execute("SELECT name, url FROM krisha_complex_catalog")
    catalog = [(cn, cu, norm(cn)) for cn, cu in cur.fetchall() if len(norm(cn)) >= 3]

    cur.execute("""
        SELECT id, name FROM complexes
        WHERE is_garbage IS NOT TRUE AND krisha_url IS NULL
          AND NOT COALESCE(source_info ? 'korter',FALSE)
          AND NOT COALESCE(source_info ? 'homsters',FALSE)
          AND developer_id IS NULL
          AND NOT EXISTS (SELECT 1 FROM apartment_listings al
                          WHERE lower(trim(al.complex_name))=lower(trim(complexes.name)))
        ORDER BY id
    """)
    empties = cur.fetchall()

    merge_plan, junk_plan, keep_plan = [], [], []
    for cid, name in empties:
        n = norm(name)
        b = base_no_num(name)

        # 1) точный норм-канон
        canon = next(((x[0], x[1]) for x in clean_norms if x[2] == n), None) or \
                next(((x[0], x[1]) for x in clean_norms if x[2] == b), None)
        if canon:
            merge_plan.append((cid, name, canon[0], canon[1]))
            continue
        # 2) точный каталог
        cat_hit = next(((x[0], x[1]) for x in catalog if x[2] == n), None) or \
                  next(((x[0], x[1]) for x in catalog if x[2] == b), None)
        if cat_hit:
            merge_plan.append((cid, name, None, cat_hit[1]))
            continue
        # 5) мусор?
        low = name.lower()
        is_junk_name = any(m in low for m in JUNK_MARKERS) or len(n) < 4
        if is_junk_name:
            junk_plan.append((cid, name, "мусорное название"))
            continue
        # 3) фаззи канон >= 0.88 (только для имён >= 5 символов)
        best = None
        if len(n) >= 5:
            for cid2, cn2, nn2 in clean_norms:
                s = max(fuzzy(n, nn2), fuzzy(b, nn2))
                if s >= 0.88 and (best is None or s > best[0]):
                    best = (s, cid2, cn2)
        if best:
            merge_plan.append((cid, name, best[1], best[2]))
            continue
        # 4) фаззи каталог >= 0.88
        best = None
        if len(n) >= 5:
            for cn2, cu2, nn2 in catalog:
                s = max(fuzzy(n, nn2), fuzzy(b, nn2))
                if s >= 0.88 and (best is None or s > best[0]):
                    best = (s, cn2, cu2)
        if best:
            merge_plan.append((cid, name, None, best[2]))
            continue
        # 6) объявления с похожей базой
        cur.execute("SELECT DISTINCT complex_name FROM apartment_listings WHERE lower(complex_name) LIKE %s OR lower(complex_name) LIKE %s LIMIT 3",
                    (b + '%', n + '%'))
        similar = [r[0] for r in cur.fetchall() if r[0] and r[0] != name]
        # 6) мусор?
        low = name.lower()
        if any(m in low for m in JUNK_MARKERS) or len(n) < 4:
            junk_plan.append((cid, name, "мусорное название"))
        elif similar:
            keep_plan.append((cid, name, "объявления: " + ", ".join(similar[:2])))
        else:
            keep_plan.append((cid, name, "реальный ЖК без объявлений"))

    print(f"=== ОБЪЕДИНИТЬ: {len(merge_plan)} ===")
    for cid, name, did, dn in merge_plan:
        print(f"  id={cid} {name:40s} -> {dn}" + (f" (canon id={did})" if did else " [КРЫША]"))
    print(f"\n=== ПОМЕТИТЬ МУСОРОМ: {len(junk_plan)} ===")
    for cid, name, why in junk_plan:
        print(f"  id={cid} {name:40s} [{why}]")
    print(f"\n=== ОСТАВИТЬ: {len(keep_plan)} ===")
    for cid, name, why in keep_plan:
        print(f"  id={cid} {name:40s} [{why}]")

    if "--apply" in sys.argv:
        for cid, name, did, dn in merge_plan:
            if did:
                cur.execute("UPDATE apartment_listings SET complex_name=%s WHERE complex_name=%s", (dn, name))
                cur.execute("UPDATE complexes SET is_garbage=TRUE, updated_at=now() WHERE id=%s", (cid,))
            else:
                cur.execute("UPDATE complexes SET krisha_url=%s, updated_at=now() WHERE id=%s", (dn, cid))
        for cid, name, why in junk_plan:
            cur.execute("UPDATE complexes SET is_garbage=TRUE, updated_at=now() WHERE id=%s", (cid,))
        conn.commit()
        print("\nПРИМЕНЕНО", flush=True)

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
