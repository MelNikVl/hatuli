#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сопоставить сайт vs CSV по номеру квартиры."""
import json, csv, re

site = json.load(open('/tmp/svoydom_astana.json'))
print(f'с сайта: {len(site)}')

def num_of(it):
    m = re.search(r'№?\s*(\d+)', it.get('name') or '')
    return int(m.group(1)) if m else None

# ключ: (complex, номер)
site_map = {}
for it in site:
    n = num_of(it)
    if n is not None:
        site_map[(it.get('complex'), n)] = it

csv_rows = []
with open('/tmp/kvartiry_full.csv', encoding='utf-8-sig') as f:
    for row in csv.DictReader(f):
        if not (row['plan_path'] or '').startswith('Astana'):
            continue
        if row['status'] != 'Свободно':
            continue
        m = re.search(r'№?\s*(\d+)', row['number'] or '')
        num = int(m.group(1)) if m else None
        try:
            area = round(float(row['area']), 1)
        except Exception:
            area = None
        rooms = 0
        if row['rooms'] and row['rooms'][0].isdigit():
            rooms = int(row['rooms'][0])
        csv_rows.append({
            'project': row['project'], 'section': row['section'], 'floor': row['floor'],
            'number': row['number'], 'num': num, 'rooms': rooms, 'area': area,
            'status': row['status'], 'price': row['price'], 'plan_path': row['plan_path'],
        })

print(f'в CSV свободных: {len(csv_rows)}')
matched = 0
no_photo = []
for r in csv_rows:
    if r['num'] is None:
        no_photo.append(r)
        continue
    key = (r['project'], r['num'])
    if key in site_map:
        it = site_map[key]
        r['photo'] = it.get('pictureFull') or it.get('picture')
        r['price_site'] = it.get('price')
        r['area_site'] = it.get('area')
        r['rooms_site'] = it.get('rooms')
        matched += 1
    else:
        no_photo.append(r)

print(f'сопоставлено с фото: {matched}')
print(f'без фото: {len(no_photo)}')
for r in no_photo[:12]:
    print(f'  {r["project"]} | {r["number"]} | {r["rooms"]}к | {r["area"]} м² | эт {r["floor"]}')

# контроль: совпадает ли площадь/цена у сопоставленных
mismatch = 0
for r in csv_rows:
    if r.get('photo') and r.get('area_site') is not None:
        if abs(float(r['area_site']) - float(r['area'])) > 1.5:
            mismatch += 1
print(f'расхождений по площади >1.5 м²: {mismatch}')

json.dump(csv_rows, open('/tmp/svoydom_matched.json', 'w'), ensure_ascii=False, indent=1)
print('сохранено /tmp/svoydom_matched.json')
