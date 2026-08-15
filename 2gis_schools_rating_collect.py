#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""2gis_schools_rating_collect.py — рейтинги школ/садиков с 2GIS (v2, Playwright).

Поиск geo_id организаций в 2GIS требует JS-рендера (поиск школ грузится
XHR). Используем headless Chromium (Playwright, уже стоит в venv для ПНЗ):
  1. render https://2gis.kz/astana/search/{name} -> geo-ссылки
  2. матчинг по названию + расстоянию (<100 м; школы крупные — до 250 м)
  3. rating + reviews_count с карточки geo (SSR-часть)

Запуск: cd ~/krisha_bot && venv/bin/python 2gis_schools_rating_collect.py --limit 80 [--fast]
"""
import argparse
import math
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import psycopg2

BASE = Path('/home/nik/krisha_bot')
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
GEO_RE = re.compile(r'href="/astana/geo/(\d+)"[^>]*>(?:\s*<[^>]+>)*\s*([^<]{2,90})<', re.S)

_pw = None  # singleton Playwright sync API


def get(url):
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept-Language': 'ru'})
    with urllib.request.urlopen(req, timeout=40) as r:
        return r.read().decode('utf-8', 'ignore')


def _pw_page():
    global _pw
    if _pw is None:
        from playwright.sync_api import sync_playwright
        _pw = sync_playwright().start()
    return _pw


def search_render(q):
    """Рендер поисковой выдачи 2GIS через Playwright -> HTML."""
    p = _pw_page()
    browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
    try:
        page = browser.new_page(user_agent=UA)
        url = 'https://2gis.kz/astana/search/' + urllib.parse.quote(q)
        page.goto(url, timeout=45000, wait_until='domcontentloaded')
        page.wait_for_timeout(6000)  # дать XHR-результатам прогрузиться
        page.wait_for_selector('a[href*="/astana/geo/"]', timeout=15000)
        return page.content()
    except Exception:
        return None
    finally:
        browser.close()


def haversine(lat1, lon1, lat2, lon2):
    if None in (lat1, lon1, lat2, lon2):
        return 1e9
    R = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def norm(s):
    n = re.sub(r'[\s\.\-_()«»"|]', '', (s or '').lower())
    for p in ('жк', 'кг', 'кд', 'мжк', 'гп'):
        if n.startswith(p) and len(n) > len(p) + 2:
            n = n[len(p):]
            break
    return n


def geo_coords(gid):
    try:
        gh = get('https://2gis.kz/astana/geo/%s' % gid)
    except Exception:
        return None
    m = re.search(r'directions/points/%7C([0-9.]+)%2C([0-9.]+)', gh)
    if not m:
        return None
    return float(m.group(2)), float(m.group(1))


def find_geo(name, lat, lon):
    """geo_id через Playwright-поиск + проверка расстояния."""
    n = norm(name)
    if len(n) < 4:
        return None
    for q in (name,):
        h = search_render(q)
        if not h:
            continue
        best = None
        for gid, title in GEO_RE.findall(h):
            t = norm(title.split(',')[0])
            if not (n in t or t in n):
                continue
            c = geo_coords(gid)
            if not c:
                continue
            d = haversine(lat, lon, c[0], c[1])
            if d < 100:
                return gid, title, d
            if best is None:
                best = (gid, title, d)
        if best and best[2] < 250:
            return best
    return None


def fetch_rating(gid):
    try:
        h = get('https://2gis.kz/astana/geo/%s' % gid)
    except Exception:
        return None, None
    rating, count = None, None
    i = h.find('оценок')
    if i > 0:
        seg = h[max(0, i - 1500):i + 100]
        m = re.search(r'([0-9]\.[0-9])', seg)
        rating = float(m.group(1)) if m else None
        mc = re.search(r'(\d+)\s+оцен', h[max(0, i - 100):i + 50])
        count = int(mc.group(1)) if mc else None
    return rating, count


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=80)
    ap.add_argument('--fast', action='store_true')
    args = ap.parse_args()
    sleep_s = 5 if args.fast else 15

    dsn = 'postgresql://krisha@localhost/krisha_bot'
    for line in (BASE / '.env').read_text(encoding='utf-8').splitlines():
        if line.startswith('DATABASE_URL='):
            dsn = line.split('=', 1)[1].strip()
    conn = psycopg2.connect(dsn)
    cur = conn.cursor()

    done = {'school': 0, 'kindergarten': 0, 'matched': 0, 'nomatch': 0}
    for table, kind in (('astana_schools', 'school'), ('astana_kindergartens', 'kindergarten')):
        cur.execute("""SELECT id, name, lat, lon FROM %s
                       WHERE name IS NOT NULL AND lat IS NOT NULL AND lon IS NOT NULL
                         AND rating_fetched_at IS NULL
                       ORDER BY id LIMIT %%s""" % table, (args.limit,))
        rows = cur.fetchall()
        print('%s: %d к обработке' % (table, len(rows)))
        for rid, name, lat, lon in rows:
            done[kind] += 1
            r = find_geo(name, lat, lon)
            if not r:
                done['nomatch'] += 1
                cur.execute("UPDATE %s SET rating_fetched_at = now() WHERE id = %%s" % table, (rid,))
                conn.commit()
                time.sleep(sleep_s)
                continue
            gid, title, dist = r
            rating, cnt = fetch_rating(gid)
            done['matched'] += 1
            cur.execute("""UPDATE %s SET rating_2gis = %%s, reviews_count_2gis = %%s,
                           geo_2gis_id = %%s, rating_fetched_at = now() WHERE id = %%s""" % table,
                        (rating, cnt, gid, rid))
            conn.commit()
            print('  #%d %s -> %s (%.0f м) rating=%s cnt=%s' % (rid, (name or '')[:34], gid, dist, rating, cnt))
            time.sleep(sleep_s)
    if _pw is not None:
        _pw.stop()
    conn.close()
    print('ИТОГ: school=%d kind=%d matched=%d nomatch=%d' % (
        done['school'], done['kindergarten'], done['matched'], done['nomatch']))
    return 0


if __name__ == '__main__':
    sys.exit(main())
