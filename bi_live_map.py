#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Привязка трансляций BI Group к ЖК + колонка live_url в complexes."""
import subprocess, json

# (id трансляции, имя ЖК на bi.group)
LIVE = [
    (126, "Nexpo Classic"), (140, "Garden View"), (181, "Aisar"),
    (182, "GreenLine. Flora"), (283, "GreenLine. Headliner Exclusive"),
    (493, "Arena Towers"), (556, "Jetisu. Kerbez"), (1027, "Turan Tower"),
    (1329, "Atlant Unique"), (1495, "GreenLine. Astra"), (1535, "AruPark"),
    (1633, "Nexpo Vision"), (1634, "Capital Park Emotions"), (2936, "Arena Unity"),
    (3118, "GreenLine. 4YOU"), (3165, "MOD Urban"), (3550, "Qasteev"),
    (3551, "Äuez"), (3924, "GreenLine. Aurora"), (3991, "YRYSTY"),
    (4098, "Arena Style"), (4319, "PARKLAND"), (4412, "GreenLine. SAKURA"),
    (4446, "Family Gardens"), (4647, "ABYROI"), (4758, "GreenLine. Verda"),
    (4759, "Äuez Park"), (4810, "Jetisu Satti"), (4812, "Jetisu Kerbez Comfort"),
    (4813, "Arena Vista"), (4814, "DARMEN"), (5499, "MOD Style"),
    (5516, "Nexpo Aura"), (5751, "GreenLine. Garden"), (5806, "MOD Ultra"),
    (5807, "IZBASAR"), (5883, "Baizaman"), (5906, "ŪIA.DARYN"),
    (6863, "Capital Park Melody"), (7143, "Bosağa"), (7164, "GreenLine. Prima"),
    (7669, "Jetisu.Aspan"), (7706, "MOD Frame"), (7961, "TŪLĞA"),
    (8199, "Capital Park Art"), (8560, "Family Nest"), (8561, "ŪIA.BIRLIK"),
    (8581, "ŪIA.TARIH"), (8772, "OİU"), (8886, "Örnek"),
    (9605, "GreenLine. Velora"), (9968, "Capital Park Joy"),
    (10033, "Capital Park Vector"), (10074, "Jetisu Bereke"),
    (10674, "Alem Sana"), (10823, "Arna Urpaq"), (10897, "Alem Zerde"),
    (11182, "Greenline. Tiara"), (11183, "MOD Fusion"),
]

def psql(sql):
    r = subprocess.run(["sudo", "-u", "postgres", "psql", "-d", "krisha_bot", "-t", "-A", "-c", sql],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:300])
    return r.stdout.strip()

# колонка live_url
psql("ALTER TABLE complexes ADD COLUMN IF NOT EXISTS live_url TEXT")
print("live_url добавлена")

# нормализация имён ЖК в нашей БД: убираем ЖК/Бигвилль префиксы, точки
def norm(name):
    import re
    n = name.lower()
    n = re.sub(r'^(жк|кг|кд|жилой комплекс|жилой массив|бигвилль|bigville)\s*', '', n)
    n = n.replace('.', ' ').replace('—', ' ').replace('-', ' ').replace('ё', 'е')
    n = re.sub(r'[^a-zа-я0-9 ]', ' ', n)
    n = re.sub(r'\s+', ' ', n).strip()
    return n

# все ЖК
rows = [l.split("\t") for l in psql(
    "SELECT id || chr(9) || name FROM complexes WHERE is_garbage IS NOT TRUE").splitlines() if l]

# индекс по нормализованным именам
idx = {}
for cid, name in rows:
    idx.setdefault(norm(name), []).append((int(cid), name))

matched, missed = [], []
for live_id, live_name in LIVE:
    key = norm(live_name)
    cands = idx.get(key) or idx.get(key.replace(' ', ''))
    if not cands:
        # частичное совпадение
        for k, v in idx.items():
            if key and (key in k or k in key) and len(key) >= 6:
                cands = v
                break
    if cands:
        cid, cname = cands[0]
        matched.append((live_id, live_name, cid, cname))
    else:
        missed.append((live_id, live_name))

print(f"Сопоставлено: {len(matched)} из {len(LIVE)}")
for live_id, live_name, cid, cname in matched:
    psql(f"UPDATE complexes SET live_url = 'https://bi.group/ru/live/{live_id}' WHERE id = {cid}")
    print(f"  ✓ {live_name} -> {cname} (id={cid})")

print(f"\nНЕ найдено в базе ({len(missed)}):")
for live_id, live_name in missed:
    print(f"  ✗ {live_name} (live/{live_id})")

print("\nЖК с live_url:", psql("SELECT COUNT(*) FROM complexes WHERE live_url IS NOT NULL"))
