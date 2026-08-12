#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""1) Пометить 15 ЖК Svoy Dom Астаны is_newbuild + developer_id=72.
2) Загрузить 1318 юнитов из svoydom_matched.json в newbuild_units."""
import subprocess, json, sys, time
sys.path.insert(0, '/home/nik/krisha_bot')

def psql(sql):
    r = subprocess.run(['sudo', '-u', 'postgres', 'psql', '-d', 'krisha_bot', '-t', '-A', '-c', sql],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:400])
    return r.stdout.strip()

# ── 1) ЖК: is_newbuild + developer 72 ──
ids = [2349, 1807, 2800, 3297, 2290, 2869, 1934, 3249, 1036, 3577, 3236, 2528, 2771, 3041, 140]
psql(f"""
    UPDATE complexes SET is_newbuild = TRUE, developer_id = 72
    WHERE id = ANY(ARRAY[{','.join(map(str, ids))}])
""")
print(f'ЖК помечено is_newbuild: {len(ids)}')

# проверить
print(psql("SELECT COUNT(*) FROM complexes WHERE is_newbuild AND developer_id = 72"))

# ── 2) юниты ──
rows = json.load(open('/tmp/svoydom_matched.json'))
with_photo = [r for r in rows if r.get('photo')]
print(f'юнитов к загрузке: {len(with_photo)}')

# маппинг ЖК: csv-имя -> id
cx_map = {'Shalqar': 2349, 'Altyn Emel': 1807, 'Aqterek': 2800, 'Aqterek 2': 3297,
          'Araily': 2290, 'Baiqadam': 2869, 'Baisal': 1934, 'Elaman': 3249,
          'Umit': 1036, 'Qadam': 3577, 'Gauhartas 2': 3236, 'Gauhartas': 2528,
          'Asyl Meken': 2771, 'Jana Qala': 3041, 'Arman Meken': 140}

# чистим старые юниты svoydom (если были)
psql("DELETE FROM newbuild_units WHERE source = 'svoydom'")
print('старые svoydom-юниты удалены')

batch = []
ins = 0
for r in with_photo:
    cid = cx_map.get(r['project'])
    if not cid:
        print(f'  нет ЖК для {r["project"]}, пропуск {r["number"]}')
        continue
    photo = r['photo']
    if photo.startswith('/'):
        photo = 'https://svoydom.kz' + photo
    price = int(r['price']) if r.get('price') else None
    floor = int(r['floor']) if str(r.get('floor') or '').isdigit() else None
    rooms = r.get('rooms') or 0
    area = r.get('area')
    section = r.get('section') or ''
    number = str(r.get('number') or '')
    # source_unit_id: svoydom-{complex}-{section}-{number}-{area} (номера/секции повторяются)
    sid = f"sv-{cid}-{section}-{number}-{area}".replace(' ', '_')
    safe_photo = photo.replace("'", "''")
    batch.append(f"({cid}, 'svoydom', '{sid}', '{section}', {floor}, {rooms}, {area}, {price}, '{safe_photo}', 'available', now(), now())")
    if len(batch) >= 100:
        vals = ','.join(batch)
        psql(f"""INSERT INTO newbuild_units (complex_id, source, source_unit_id, section, floor, rooms, area, price, layout_photo_url, status, first_seen_at, last_seen_at)
                 VALUES {vals}""")
        ins += len(batch)
        batch = []
if batch:
    vals = ','.join(batch)
    psql(f"""INSERT INTO newbuild_units (complex_id, source, source_unit_id, section, floor, rooms, area, price, layout_photo_url, status, first_seen_at, last_seen_at)
             VALUES {vals}""")
    ins += len(batch)

print(f'юнитов вставлено: {ins}')
print(psql("SELECT COUNT(*) FROM newbuild_units WHERE source = 'svoydom'"))
