#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Собрать адрес + срок сдачи со страниц svoydom.kz для 15 ЖК."""
import json, re, time, urllib.request

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0'

def fetch(url):
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read().decode('utf-8', 'replace')

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
}

ROMAN = {'I': 1, 'II': 2, 'III': 3, 'IV': 4, 'V': 5}
def parse_deadline(txt):
    # "I КВАРТАЛ 2027 ГОД" / "4 квартал 2026" / "2027"
    m = re.search(r'([IVX]+)\s*КВАРТАЛ\s*(\d{4})', txt, re.I)
    if m:
        return int(m.group(2)), ROMAN.get(m.group(1).upper(), 0)
    m = re.search(r'(\d)\s*КВАРТАЛ\s*(\d{4})', txt, re.I)
    if m:
        return int(m.group(2)), int(m.group(1))
    m = re.search(r'(\d{4})\s*(?:ГОД|ГОДА|г\.?)', txt, re.I)
    if m:
        return int(m.group(1)), None
    return None, None

out = {}
for cid, url in pages.items():
    try:
        t = fetch(url)
        txt = re.sub(r'<[^>]+>', ' ', t)
        txt = re.sub(r'\s+', ' ', txt)
        # адрес
        addr = None
        m = re.search(r'Адрес[:]?\s*(г\.?\s*Астана[^»]{5,120})', txt)
        if not m:
            m = re.search(r'Адрес[:]?\s*([^»]{5,120}?)(?:\.|</|ОСТАВИТЬ)', txt)
        if m:
            addr = re.sub(r'\s+', ' ', m.group(1)).strip().strip('.').strip()
        # срок сдачи
        year, q = None, None
        for pat in [r'СРОК СДАЧИ[:]?\s*([^»]{2,60})', r'Срок сдачи[:]?\s*([^»]{2,60})', r'срок сдачи[:]?\s*([^»]{2,60})']:
            m = re.search(pat, txt)
            if m:
                year, q = parse_deadline(m.group(1))
                if year:
                    break
        if not year:
            year, q = parse_deadline(txt)
        out[cid] = {'url': url, 'address': addr, 'year': year, 'quarter': q}
        print(f'{cid}: год={year} кв={q} | адрес={addr}', flush=True)
    except Exception as e:
        out[cid] = {'url': url, 'error': str(e)}
        print(f'{cid}: ошибка {e}', flush=True)
    time.sleep(0.6)

json.dump(out, open('/tmp/svoydom_info.json', 'w'), ensure_ascii=False, indent=1)
print('сохранено /tmp/svoydom_info.json')
