#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сопоставить квартиры с сайта (svoydom_astana.json) с CSV (полные данные)."""
import json, csv

# квартиры с сайта
site = json.load(open('/tmp/svoydom_astana.json'))
print(f'с сайта: {len(site)}')

# ключ: (complex, floor, area, rooms) — номер может отличаться
def site_key(it):
    return (it.get('complex'), it.get('floor'), round(float(it.get('area') or 0), 1), it.get('rooms'))

site_map = {}
for it in site:
    site_map[site_key(it)] = it

# CSV
csv_rows = []
with open('/tmp/kvartiry_full.csv', encoding='utf-8-sig') as f:
    for row in csv.DictReader(f):
        if not (row['plan_path'] or '').startswith('Astana'):
            continue
        if row['status'] != 'Свободно':
            continue
        try:
            area = round(float(row['area']), 1)
        except Exception:
            continue
        rooms = len(row['rooms']) if row['rooms'] and row['rooms'][0].isdigit() else 0
        if isinstance(row['rooms'], str) and row['rooms'][0].isdigit():
            rooms = int(row['rooms'][0])
        csv_rows.append({
            'project': row['project'], 'section': row['section'], 'floor': row['floor'],
            'number': row['number'], 'rooms': rooms, 'area': area,
            'status': row['status'], 'price': row['price'], 'plan_path': row['plan_path'],
        })

print(f'в CSV свободных: {len(csv_rows)}')
matched = 0
no_photo = []
for r in csv_rows:
    key = (r['project'], r['floor'], r['area'], r['rooms'])
    if key in site_map:
        r['photo'] = site_map[key].get('pictureFull') or site_map[key].get('picture')
        matched += 1
    else:
        no_photo.append(r)

print(f'сопоставлено с фото: {matched}')
print(f'без фото: {len(no_photo)}')
for r in no_photo[:10]:
    print(f'  {r["project"]} | №{r["number"]} | {r["rooms"]}к | {r["area"]} м² | эт {r["floor"]}')

# проверить: у скольких с сайта НЕТ пары в CSV (лишние)
csv_keys = set()
for r in csv_rows:
    csv_keys.add((r['project'], r['floor'], r['area'], r['rooms']))
extra = [it for it in site if site_key(it) not in csv_keys]
print(f'с сайта без пары в CSV: {len(extra)}')
for it in extra[:10]:
    print(f'  {it.get("complex")} | {it.get("name")} | {it.get("rooms")}к | {it.get("area")} м² | эт {it.get("floor")}')

json.dump(csv_rows, open('/tmp/svoydom_matched.json', 'w'), ensure_ascii=False, indent=1)
print('сохранено /tmp/svoydom_matched.json')
