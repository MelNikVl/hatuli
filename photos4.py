#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Фото с Крыши для ЖК без фото: Бейбарыс, safar, Tumar Exclusive, Вдоль ручья Сарыбулак."""
import subprocess, re, json, time

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0"

def curl(url):
    r = subprocess.run(["curl", "-s", "-L", "-A", UA, "--max-time", "25", url], capture_output=True, text=True)
    return r.stdout

def psql(sql):
    r = subprocess.run(['sudo', '-u', 'postgres', 'psql', '-d', 'krisha_bot', '-t', '-A', '-c', sql],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:300])
    return r.stdout.strip()

# (id ЖК, slug на Крыше)
TARGETS = [
    (1336, "beybarys"),          # Бейбарыс
    (223, "safar"),              # safar
    (2891, "tumarexclusive"),    # Tumar Exclusive (slug может отличаться)
    (2807, "vip-gorodok-saranda"),  # Вдоль ручья Сарыбулак — проверю поиском
]

def get_photos(html):
    # полноразмерные content-фото
    photos = re.findall(r'https://krisha-photos\.kcdn\.online/content/[^"\'\s]+\.(?:jpg|jpeg|png)', html)
    # галерея 750x470
    photos += re.findall(r'https://krisha-photos\.kcdn\.online/[^"\'\s]+photo-750x470\.(?:jpg|jpeg|png)', html)
    # дедуп, максимум 10
    out = []
    for p in photos:
        if p not in out:
            out.append(p)
    return out[:10]

for cid, slug in TARGETS:
    # пробуем прямой slug, потом поиск
    html = curl(f"https://krisha.kz/complex/show/astana/{slug}/")
    photos = get_photos(html)
    if not photos:
        # поиск
        shtml = curl(f"https://krisha.kz/complex/search/astana/?query={slug}")
        m = re.search(r'complex/show/astana/([a-z0-9-]+)', shtml)
        if m and m.group(1) != slug:
            html = curl(f"https://krisha.kz/complex/show/astana/{m.group(1)}/")
            photos = get_photos(html)
    if photos:
        arr = json.dumps(photos, ensure_ascii=False).replace("'", "''")
        psql(f"UPDATE complexes SET photos = '{arr}'::jsonb WHERE id = {cid}")
        print(f"  {cid}: {len(photos)} фото")
        for p in photos[:3]:
            print(f"    {p}")
    else:
        print(f"  {cid}: фото не найдены")
    time.sleep(1.2)
