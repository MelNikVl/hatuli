#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Обновить 15 ЖК Svoy Dom: год сдачи, адрес, описание."""
import json, subprocess, re

def psql(sql):
    r = subprocess.run(['sudo', '-u', 'postgres', 'psql', '-d', 'krisha_bot', '-t', '-A', '-c', sql],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:400])
    return r.stdout.strip()

info = json.load(open('/tmp/svoydom_info.json'))

# описания ЖК (краткие, из данных)
DESC = {
    2349: 'ЖК Shalqar от застройщика Svoy Dom (Астана). Срок сдачи: IV квартал 2026.',
    1807: 'ЖК Altyn Emel от застройщика Svoy Dom. Проспект Аль-Фараби, 7/3.',
    2800: 'ЖК Aqterek от Svoy Dom. Район Нура, улица Кайым Мухамедханов, 43/1. Сдача: IV кв. 2026.',
    3297: 'ЖК Aqterek 2 от Svoy Dom. Район Нура, улица Кайым Мухамедханов, 43. Сдача: IV кв. 2026.',
    2290: 'ЖК Araily от Svoy Dom. Район Сарайшык. Сдача: III кв. 2026.',
    2869: 'ЖК Baiqadam от Svoy Dom. Район Сарайшык, улица Жумекен Нажимеденов, 6. Сдача: IV кв. 2026.',
    1934: 'ЖК Baisal от Svoy Dom. Район Сарайшык, улица Жумекен Нажимеденов, 5Б. 8 секций, 9-12 этажей, 378 квартир. Кирпичный дом, надземный паркинг, потолки 3 м. Сдача: I кв. 2027.',
    3249: 'ЖК Elaman от Svoy Dom. Р-н Сарайшык, ул. Ш. Калдаякова, 53. 5 секций, 9 этажей. Сдача: I кв. 2027.',
    1036: 'ЖК UMIT от Svoy Dom (Астана). Сдача: III кв. 2026.',
    3577: 'ЖК Qadam от Svoy Dom. Первая линия ул. Чингиза Айтматова. Сдача: IV кв. 2026.',
    3236: 'ЖК Gauhartas 2 от Svoy Dom. Пересечение пр. Улы дала и ул. Казыбек Би. Сдача: III кв. 2026.',
    2528: 'ЖК Gauhartas от Svoy Dom (Астана). Сдача: III кв. 2026.',
    2771: 'ЖК Asyl Meken от Svoy Dom. Р-н Нура, пересечение улиц Ч. Айтматова и К. Мухамедханова. 3 секции, 12 этажей. Сдача: I кв. 2027.',
    3041: 'ЖК Jana Qala от Svoy Dom (Астана). Сдача: I кв. 2027.',
    140: 'ЖК Arman Meken от Svoy Dom. Пересечение Мухамедханова - Айтматова. Сдача: I кв. 2027.',
}

for cid, d in info.items():
    year = d.get('year')
    q = d.get('quarter')
    addr = d.get('address')
    if addr:
        # почистить: оборвать на "ОСТАВИТЬ"/"Телефон"/"Срок"
        addr = re.split(r'\s*(?:ОСТАВИТЬ|СКАЧАТЬ|Телефон|Срок|О проекте|Количество|$)', addr)[0].strip().rstrip(',')
    sets = []
    if year:
        sets.append(f"completion_year = {year}")
    if q:
        sets.append(f"completion_quarter = {q}")
    if addr:
        safe = addr.replace("'", "''")
        sets.append(f"address = '{safe}'")
    desc = DESC.get(int(cid))
    if desc:
        safe_d = desc.replace("'", "''")
        sets.append(f"description = '{safe_d}'")
    if sets:
        psql(f"UPDATE complexes SET {', '.join(sets)} WHERE id = {cid}")
        print(f'{cid}: {", ".join(sets[:3])}')

print('готово')
