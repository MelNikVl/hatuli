#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Дополняем complex_materials для 10 ЖК из списка 50, у которых данных не было."""
import subprocess

def psql(sql: str) -> str:
    r = subprocess.run(["sudo", "-u", "postgres", "psql", "-d", "krisha_bot", "-t", "-A", "-c", sql],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:300])
    return r.stdout.strip()

# complex_id: (facade, walls, windows, elevators, heating, doors, notes, source_name, source_url)
DATA = {
    3221: (  # Nexpo Aura
        "Фасад — алюминиевые панели Sevalcon (Европа); каркас — монолитный железобетон",
        "Межквартирные стены — Acoustic Pro; межкомнатные — газоблок",
        None, "Бесшумные лифты", None, None,
        "Авторский дизайн INK Architects; премиальные материалы; удобная навигация, колясочные",
        "krisha / metry.kz", "https://m.krisha.kz/complex/show/astana/nexpoaura/"),
    3741: (  # Jetisu.Aspan
        "Фасад — современные материалы европейского производства, экологичные",
        None, None, None, None, None,
        "Бигвилль Jetisu (Kerbez/Satti/Aqsu): витражные окна на первых этажах (Satti); экологические материалы",
        "bi.group", "https://bi.group/ru/landing/jetisu-kerbez"),
    2506: (  # Expo Plaza
        "Фасад — натуральный камень + стилизованные под дерево панели; монолитно-каркасная технология",
        None, None, None, None, None,
        "BAZIS-А; Есильский район, пр. Кабанбай батыра; введён в эксплуатацию",
        "korter / krisha", "https://korter.kz/жк-expo-plaza"),
    2722: (  # Highvill Astana
        "Фасад — натуральные материалы и гранит (премиум)",
        None, None, "Грузовой, пассажирский", None, None,
        "Премиум-класс; двухуровневый паркинг; ул. Ж. Нажимеденова",
        "krisha / youtube", "https://m.krisha.kz/complex/show/highvill-ishim/"),
    1982: (  # Highvill Ishim
        "Фасад — натуральные материалы; монолитный",
        None, None, "Грузовой, пассажирский", None, None,
        "Комфорт; 28 эт.; потолки 3 м; 342 квартиры; надземный паркинг; отделка чистовая",
        "krisha", "https://m.krisha.kz/complex/show/highvill-ishim/"),
    850: (  # England
        "Лифтовые холлы облицованы дорогими материалами; монолитно-каркасная технология",
        None, None, "Скоростные бесшумные лифты импортного производства", None, None,
        "BAZIS-А; 8 домов, 7/9 этажей; подземный паркинг",
        "krisha / korter", "https://m.krisha.kz/complex/show/england/"),
    1786: (  # Tasty
        None, None, None, None, None, None,
        "Застройщик — уточнить; данные по материалам не найдены в открытых источниках",
        "—", None),
    2106: (  # Sharyn
        "Фасад — вентилируемый, фиброцементные панели Profib (по krisha — облицовочный кирпич)",
        None, None, None, None, None,
        "SVOY DOM; 10/12 эт.; потолки 2,7 м; 454 квартиры; видеонаблюдение, пропускная система",
        "kapster / krisha", "https://krisha.kz/complex/show/astana/sharyn/"),
    132: (  # alatau park
        None, None, None, None, None, None,
        "Материалы уточнить (быстрый поиск не дал деталей)",
        "—", None),
    3503: (  # Parkland F
        None, None, None, None, None, None,
        "Материалы уточнить (быстрый поиск не дал деталей)",
        "—", None),
}

def esc(v):
    return v.replace("'", "''") if v else None

n = 0
for cid, (facade, walls, windows, elev, heat, doors, notes, src, url) in DATA.items():
    cols, vals = [], []
    for col, v in [("facade", facade), ("walls", walls), ("windows", windows),
                   ("elevators", elev), ("heating", heat), ("doors", doors),
                   ("notes", notes), ("source_name", src), ("source_url", url)]:
        cols.append(col)
        vals.append(f"'{esc(v)}'" if v else "NULL")
    sql = (f"INSERT INTO complex_materials (complex_id, {', '.join(cols)}) "
           f"VALUES ({cid}, {', '.join(vals)}) "
           f"ON CONFLICT (complex_id, source_name) DO NOTHING")
    psql(sql)
    n += 1
    print(f"✓ id={cid} добавлено")

print(f"\nВсего добавлено: {n}")
print(psql("SELECT COUNT(*) FROM complex_materials"))
