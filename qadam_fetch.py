#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Докачать Qadam (522) + сопоставить сайт vs CSV."""
import json, re, time, urllib.request, urllib.parse

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0'

def fetch(url, data=None, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers={
                'User-Agent': UA, 'Content-Type': 'application/x-www-form-urlencoded',
                'Accept-Language': 'ru-RU,ru;q=0.9', 'X-Requested-With': 'XMLHttpRequest',
                'Referer': 'https://svoydom.kz/',
            })
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode('utf-8', 'replace')
        except Exception as e:
            print(f'    попытка {i+1}: {e}', flush=True)
            time.sleep(3)
    raise RuntimeError('fetch failed')

name, page_url = 'Qadam', 'https://svoydom.kz/comfort_projects/qadam/'
html = fetch(page_url)
sessid = re.search(r'sessid["\':\s]+([a-f0-9]{32})', html).group(1)
m = re.search(r":initial-apartments='(.*?)'\s", html, re.S)
raw = m.group(1).replace('&quot;', '"').replace('&amp;', '&').replace('&#039;', "'")
d = json.loads(raw)
items = list(d.get('items', []))
pag = d.get('pagination', {})
total_pages = pag.get('totalPages', 1)
m2 = re.search(r':iblock-id="(\d+)"', html)
iblock = m2.group(1) if m2 else None
m3 = re.search(r'mode="([^"]+)"', html)
mode = m3.group(1) if m3 else 'apartments'
print(f'Qadam: {total_pages} стр, iblock={iblock}, mode={mode}')
for page in range(2, total_pages + 1):
    params = urllib.parse.urlencode({
        'action': 'getApartments', 'iblock_id': iblock, 'page_size': 12,
        'mode': mode, 'page': page, 'sessid': sessid or '',
    })
    r = fetch('https://svoydom.kz/local/components/custom/apartments.filter/ajax.php', params.encode())
    dd = json.loads(r)
    items.extend(dd.get('items', []))
    time.sleep(0.5)
    if page % 5 == 0:
        print(f'  ...{page}/{total_pages}, всего {len(items)}')
print(f'Qadam: {len(items)} квартир')

# загрузить уже собранное и добавить Qadam
all_items = json.load(open('/tmp/svoydom_astana.json'))
# убрать старый Qadam если был
all_items = [it for it in all_items if it.get('complex') != 'Qadam']
for it in items:
    it['complex'] = 'Qadam'
all_items.extend(items)
json.dump(all_items, open('/tmp/svoydom_astana.json', 'w'), ensure_ascii=False, indent=1)
print(f'ИТОГО с сайта: {len(all_items)}')

# статистика по ЖК
from collections import Counter
c = Counter(it['complex'] for it in all_items)
for k, v in sorted(c.items(), key=lambda x: -x[1]):
    print(f'  {k:15} {v}')
