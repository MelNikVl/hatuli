#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SAT-NS: photo_url ЖК с sat-ns.kz (/complex/<slug>/ -> slides/*.webp)."""
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

html = fetch('https://sat-ns.kz/ru')
slugs = sorted(set(
    re.findall(r'href="(?:https://sat-ns\.kz)?/?(?:ru/)?complex/([a-z0-9-]+)/?"', html)
))
print('slug-ов:', len(slugs), slugs[:20])

rows = psql("SELECT id || chr(9) || name FROM complexes WHERE developer_id = 7 AND is_garbage IS NOT TRUE")
updated = 0
for line in rows.splitlines():
    if not line:
        continue
    cid, cname = line.split('\t', 1)
    cid = int(cid)
    norm = re.sub(r'[^a-z0-9]+', '-', cname.lower().strip()).strip('-')
    slug = None
    for c in [norm, norm.split('-')[0]]:
        if c in slugs:
            slug = c
            break
        for s in slugs:
            if s.startswith(c) or c.startswith(s) or c in s or s in c:
                slug = s
                break
        if slug:
            break
    if not slug:
        print(f'{cid} {cname}: slug не найден', flush=True)
        continue
    try:
        p = fetch(f'https://sat-ns.kz/ru/complex/{slug}/')
        m = re.search(r'(?:src|data-src)="(https://sat-ns\.kz[^"]*/slides/[^"]+\.(?:webp|jpg|jpeg))"', p)
        if not m:
            m = re.search(r'(?:src|data-src)="(/[^"]*/slides/[^"]+\.(?:webp|jpg|jpeg))"', p)
            photo = 'https://sat-ns.kz' + m.group(1) if m else None
        else:
            photo = m.group(1)
        if photo:
            psql(f"UPDATE complexes SET photo_url = '{photo.replace(chr(39), chr(39)+chr(39))}' WHERE id = {cid}")
            updated += 1
            print(f'{cid} {cname} -> {photo[:90]}', flush=True)
        else:
            print(f'{cid} {cname}: нет slides на /complex/{slug}', flush=True)
    except Exception as e:
        print(f'{cid} {cname}: ошибка {e}', flush=True)
    time.sleep(0.5)
print(f'\nобновлено SAT-NS: {updated}')
