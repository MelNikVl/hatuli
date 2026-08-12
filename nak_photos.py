#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NAK: photo_url ЖК с сайта nak.kz (/projects/<slug> -> og:image)."""
import re, subprocess, time, urllib.request, urllib.parse

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

# каталог проектов: slug -> (title)
html = fetch('https://www.nak.kz/projects')
slugs = sorted(set(re.findall(r'href="/projects/([a-z0-9_-]+)"', html)))
print('slug-ов на /projects:', len(slugs))

rows = psql("SELECT id || chr(9) || name FROM complexes WHERE developer_id = 73 AND is_garbage IS NOT TRUE")
updated = 0
for line in rows.splitlines():
    if not line:
        continue
    cid, cname = line.split('\t', 1)
    cid = int(cid)
    # нормализация для поиска slug: 'Арай-3' -> arai-3 и т.п.
    norm = cname.lower().strip()
    norm = re.sub(r'[^a-z0-9]+', '-', norm).strip('-')
    norm = re.sub(r'^-+|-+$', '', norm)
    # кандидаты: точный, без суффиксов
    cands = [norm]
    cands.append(norm.split('-')[0])
    slug = None
    for c in cands:
        if c in slugs:
            slug = c
            break
        # префиксное совпадение (ЖК->slug)
        for s in slugs:
            if s.startswith(c) or c.startswith(s):
                slug = s
                break
        if slug:
            break
    if not slug:
        print(f'{cid} {cname}: slug не найден', flush=True)
        continue
    try:
        p = fetch(f'https://www.nak.kz/projects/{slug}')
        m = re.search(r'og:image"\s+content="([^"]+)"', p)
        if m:
            photo = m.group(1)
            psql(f"UPDATE complexes SET photo_url = '{photo.replace(chr(39), chr(39)+chr(39))}' WHERE id = {cid}")
            updated += 1
            print(f'{cid} {cname} -> {photo[:90]}', flush=True)
        else:
            print(f'{cid} {cname}: нет og:image на /projects/{slug}', flush=True)
    except Exception as e:
        print(f'{cid} {cname}: ошибка {e}', flush=True)
    time.sleep(0.6)
print(f'\nобновлено NAK: {updated}')
