#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сбор квартир Svoy Dom (Астана) с сайта svoydom.kz + CSV-файла.
Страницы ЖК: svoydom.kz/lp/astana/<slug>/ — из :initial-apartments берём
первую страницу, дальше грузим ajax.php (action=getApartments)."""
import json, re, sys, time, urllib.request, urllib.parse, csv

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0'

def fetch(url, data=None):
    req = urllib.request.Request(url, data=data, headers={
        'User-Agent': UA, 'Content-Type': 'application/x-www-form-urlencoded',
        'Accept-Language': 'ru-RU,ru;q=0.9', 'X-Requested-With': 'XMLHttpRequest',
        'Referer': 'https://svoydom.kz/',
    })
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read().decode('utf-8', 'replace')

def get_sessid(html):
    m = re.search(r'sessid["\':\s]+([a-f0-9]{32})', html)
    return m.group(1) if m else None

def scrape_complex(slug, name):
    """Скачать все квартиры ЖК с сайта."""
    url = f'https://svoydom.kz/lp/astana/{slug}/'
    html = fetch(url)
    sessid = get_sessid(html)
    m = re.search(r":initial-apartments='(.*?)'\s", html, re.S)
    if not m:
        return None
    raw = m.group(1).replace('&quot;', '"').replace('&amp;', '&').replace('&#039;', "'")
    d = json.loads(raw)
    items = list(d.get('items', []))
    pag = d.get('pagination', {})
    total_pages = pag.get('totalPages', 1)
    # iblock id и mode из страницы
    m2 = re.search(r'apt-block-iblock-id[^>]*>(\d+)', html)
    iblock = m2.group(1) if m2 else '121'
    m3 = re.search(r'apt-block-city[^>]*>(\w+)', html)
    city = m3.group(1) if m3 else 'astana'
    m4 = re.search(r"mode['\"]?\s*[:=]\s*['\"]([^'\"]+)", html)
    mode = m4.group(1) if m4 else 'default'
    if not m4:
        m4 = re.search(r'data-mode="([^"]+)"', html)
        mode = m4.group(1) if m4 else 'default'
    print(f'  {name}: 1/{total_pages} стр, iblock={iblock}, city={city}, mode={mode}, sessid={bool(sessid)}', flush=True)
    # до-гружаем остальные страницы
    for page in range(2, total_pages + 1):
        params = urllib.parse.urlencode({
            'action': 'getApartments', 'iblock_id': iblock, 'page_size': 12,
            'mode': mode, 'page': page, 'sessid': sessid or '',
        })
        try:
            r = fetch('https://svoydom.kz/local/components/custom/apartments.filter/ajax.php', params.encode())
            dd = json.loads(r)
            items.extend(dd.get('items', []))
            time.sleep(0.5)
        except Exception as e:
            print(f'    стр {page}: ошибка {e}', flush=True)
        if page % 5 == 0:
            print(f'    ...{page}/{total_pages} стр, всего {len(items)}', flush=True)
    return items

def main():
    # ЖК Астаны из CSV + slug с сайта
    projects = {
        'Shalqar': 'shalqar', 'Altyn Emel': 'altyn_emel', 'Aqterek': 'aqterek',
        'Jar-Jar': 'jar_jar', 'Araily': 'araily', 'Aqterek 2': 'aqterek',
        'Baiqadam': 'baiqadam', 'Baisal': 'baisal', 'Elaman': 'elaman',
        'Umit': 'umit', 'Qadam': 'qadam', 'Gauhartas 2': 'gauhartas',
        'Asyl Meken': 'asyl_meken', 'Jana Qala': 'jana_qala',
        'Gauhartas': 'gauhartas', 'Arman Meken': 'arman_meken',
    }
    all_items = []
    for name, slug in projects.items():
        try:
            items = scrape_complex(slug, name)
            if items is None:
                print(f'  {name}: страница не распарсилась', flush=True)
                continue
            for it in items:
                it['complex'] = name
                it['slug'] = slug
            all_items.extend(items)
            print(f'  {name}: {len(items)} квартир', flush=True)
        except Exception as e:
            print(f'  {name}: ошибка {e}', flush=True)
        time.sleep(0.8)
    print(f'\nИТОГО квартир с сайта: {len(all_items)}', flush=True)
    json.dump(all_items, open('/tmp/svoydom_astana.json', 'w'), ensure_ascii=False, indent=1)
    print('сохранено в /tmp/svoydom_astana.json', flush=True)

if __name__ == '__main__':
    main()
