#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Highvill: photo_url ЖК с highvill.kz (projects/view?id=N -> sliders)."""
import re, subprocess, time, urllib.request

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0'

def psql(sql):
    r = subprocess.run(['sudo', '-u', 'postgres', 'psql', '-d', 'krisha_bot', '-t', '-A', '-c', sql],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:400])
    return r.stdout.strip()

def fetch(url):
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read().decode('utf-8', 'replace')

# собрать (title -> первое фото) по всем id
projects = {}
for i in range(1, 8):
    try:
        p = fetch(f'https://highvill.kz/projects/view?id={i}')
        m = re.search(r'<title>([^<]+)</title>', p)
        title = m.group(1).strip() if m else f'id{i}'
        ph = re.search(r'(?:src|data-src)="(https://admin\.highvill\.kz/media/[^"]+\.(?:jpg|jpeg|png|webp))"', p)
        if ph:
            projects[title.lower()] = ph.group(1)
        print(f'id{i}: {title[:40]} -> {ph.group(1)[:60] if ph else "нет"}', flush=True)
    except Exception as e:
        print(f'id{i}: ошибка {e}', flush=True)
    time.sleep(0.5)

rows = psql("SELECT id || chr(9) || name FROM complexes WHERE developer_id = 125 AND is_garbage IS NOT TRUE")
updated = 0
for line in rows.splitlines():
    if not line:
        continue
    cid, cname = line.split('\t', 1)
    cid = int(cid)
    # найти title, содержащий часть названия
    key = cname.lower().strip()
    found = None
    for t, ph in projects.items():
        if key.split()[0][:5] in t or t[:10] in key:
            found = ph
            break
    if not found:
        print(f'{cid} {cname}: не сопоставлено', flush=True)
        continue
    psql(f"UPDATE complexes SET photo_url = '{found.replace(chr(39), chr(39)+chr(39))}' WHERE id = {cid}")
    updated += 1
    print(f'{cid} {cname} -> {found[:70]}', flush=True)
print(f'\nобновлено Highvill: {updated}')
