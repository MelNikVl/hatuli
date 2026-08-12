#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Обновить photo_url ЖК Svoy Dom с сайта застройщика (первое фото со страницы ЖК)."""
import json, re, subprocess, time, urllib.request

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

# страницы ЖК Svoy Dom: id -> url
pages = {
    2349: 'https://svoydom.kz/comfort_projects/shalqar/',
    1807: 'https://svoydom.kz/comfort_projects/altyn_emel/',
    2800: 'https://svoydom.kz/lp/astana/aqterek/',
    3297: 'https://svoydom.kz/lp/astana/aqterek_2/',
    2290: 'https://svoydom.kz/comfort_projects/araily/',
    2869: 'https://svoydom.kz/lp/astana/baiqadam/',
    1934: 'https://svoydom.kz/lp/astana/baisal/',
    3249: 'https://svoydom.kz/lp/astana/elaman/',
    1036: 'https://svoydom.kz/comfort_projects/umit/',
    3577: 'https://svoydom.kz/comfort_projects/qadam/',
    3236: 'https://svoydom.kz/comfort_projects/gauhartas/',
    2528: 'https://svoydom.kz/comfort_projects/gauhartas1/',
    2771: 'https://svoydom.kz/lp/astana/asyl_meken/',
    3041: 'https://svoydom.kz/business_projects/janaqala/',
    140:  'https://svoydom.kz/lp/astana/arman_meken/',
    2710: 'https://svoydom.kz/comfort_projects/baidaulet/',
}

# для остальных ЖК ищем страницу через sitemap/catalog
def find_page(cname):
    """Попытаться найти страницу ЖК на svoydom.kz."""
    slug = cname.lower().strip()
    slug = re.sub(r'[^a-z0-9]+', '_', slug).strip('_')
    for prefix in ['https://svoydom.kz/lp/astana/', 'https://svoydom.kz/comfort_projects/', 'https://svoydom.kz/business_projects/']:
        u = prefix + slug + '/'
        try:
            r = urllib.request.urlopen(urllib.request.Request(u, headers={'User-Agent': UA}), timeout=12)
            if r.status == 200:
                return u
        except Exception:
            pass
        time.sleep(0.4)
    return None

def first_photo(html):
    """Первое data-src изображение из слайдера (не логотип, не иконка)."""
    m = re.search(r'data-src="(/upload/landing/[^"]+)"', html)
    return 'https://svoydom.kz' + m.group(1) if m else None

rows = psql("SELECT id || chr(9) || name FROM complexes WHERE developer_id = 72 AND is_garbage IS NOT TRUE AND is_street IS NOT TRUE")
updated = 0
skipped = []
for line in rows.splitlines():
    if not line:
        continue
    cid, cname = line.split('\t', 1)
    cid = int(cid)
    url = pages.get(cid) or find_page(cname)
    if not url:
        skipped.append((cid, cname, 'нет страницы'))
        print(f'{cid} {cname}: НЕТ страницы', flush=True)
        continue
    try:
        html = fetch(url)
        photo = first_photo(html)
        if photo:
            psql(f"UPDATE complexes SET photo_url = '{photo.replace(chr(39), chr(39)+chr(39))}' WHERE id = {cid}")
            updated += 1
            print(f'{cid} {cname}: {photo[:90]}', flush=True)
        else:
            skipped.append((cid, cname, 'нет фото'))
            print(f'{cid} {cname}: нет фото на странице', flush=True)
    except Exception as e:
        skipped.append((cid, cname, str(e)[:60]))
        print(f'{cid} {cname}: ошибка {e}', flush=True)
    time.sleep(0.6)

print(f'\nобновлено: {updated}, пропущено: {len(skipped)}')
for s in skipped:
    print('  ', s)
