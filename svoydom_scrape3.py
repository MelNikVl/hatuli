#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сбор квартир Svoy Dom (Астана) — v3: iblock/mode из :iblock-id/mode атрибутов."""
import json, re, time, urllib.request, urllib.parse

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

def scrape_complex(name, page_url):
    html = fetch(page_url)
    sessid = get_sessid(html)
    m = re.search(r":initial-apartments='(.*?)'\s", html, re.S)
    if not m:
        print(f'  {name}: страница без шахматки ({page_url})', flush=True)
        return None
    raw = m.group(1).replace('&quot;', '"').replace('&amp;', '&').replace('&#039;', "'")
    d = json.loads(raw)
    items = list(d.get('items', []))
    pag = d.get('pagination', {})
    total_pages = pag.get('totalPages', 1)
    # iblock: сначала :iblock-id атрибут тега, потом apt-block-iblock-id
    m2 = re.search(r':iblock-id="(\d+)"', html)
    if not m2:
        m2 = re.search(r'apt-block-iblock-id[^>]*>(\d+)', html)
    iblock = m2.group(1) if m2 else None
    m3 = re.search(r'mode="([^"]+)"', html)
    mode = m3.group(1) if m3 else 'apartments'
    print(f'  {name}: {total_pages} стр, iblock={iblock}, mode={mode}', flush=True)
    for page in range(2, total_pages + 1):
        params = urllib.parse.urlencode({
            'action': 'getApartments', 'iblock_id': iblock, 'page_size': 12,
            'mode': mode, 'page': page, 'sessid': sessid or '',
        })
        try:
            r = fetch('https://svoydom.kz/local/components/custom/apartments.filter/ajax.php', params.encode())
            dd = json.loads(r)
            items.extend(dd.get('items', []))
            time.sleep(0.4)
        except Exception as e:
            print(f'    стр {page}: ошибка {e}', flush=True)
    return items

def main():
    projects = [
        ('Shalqar', 'https://svoydom.kz/comfort_projects/shalqar/'),
        ('Altyn Emel', 'https://svoydom.kz/comfort_projects/altyn_emel/'),
        ('Aqterek', 'https://svoydom.kz/lp/astana/aqterek/'),
        ('Aqterek 2', 'https://svoydom.kz/lp/astana/aqterek_2/'),
        ('Araily', 'https://svoydom.kz/comfort_projects/araily/'),
        ('Baiqadam', 'https://svoydom.kz/lp/astana/baiqadam/'),
        ('Baisal', 'https://svoydom.kz/lp/astana/baisal/'),
        ('Elaman', 'https://svoydom.kz/lp/astana/elaman/'),
        ('Umit', 'https://svoydom.kz/comfort_projects/umit/'),
        ('Qadam', 'https://svoydom.kz/comfort_projects/qadam/'),
        ('Gauhartas 2', 'https://svoydom.kz/comfort_projects/gauhartas/'),
        ('Gauhartas', 'https://svoydom.kz/comfort_projects/gauhartas1/'),
        ('Asyl Meken', 'https://svoydom.kz/lp/astana/asyl_meken/'),
        ('Jana Qala', 'https://svoydom.kz/business_projects/janaqala/'),
        ('Arman Meken', 'https://svoydom.kz/lp/astana/arman_meken/'),
    ]
    all_items = []
    for name, url in projects:
        try:
            items = scrape_complex(name, url)
            if items is None:
                continue
            for it in items:
                it['complex'] = name
            all_items.extend(items)
            print(f'  {name}: {len(items)}', flush=True)
        except Exception as e:
            print(f'  {name}: ошибка {e}', flush=True)
        time.sleep(0.6)
    print(f'\nИТОГО: {len(all_items)}', flush=True)
    json.dump(all_items, open('/tmp/svoydom_astana.json', 'w'), ensure_ascii=False, indent=1)

if __name__ == '__main__':
    main()
