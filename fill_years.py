#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Собрать completion_year для 14 ЖК без года (Крыша -> homsters -> korter)."""
import subprocess, re, json, time, urllib.request

def psql(sql):
    r = subprocess.run(['sudo', '-u', 'postgres', 'psql', '-d', 'krisha_bot', '-t', '-A', '-c', sql],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:300])
    return r.stdout.strip()

# ЖК без года
rows = []
for l in psql("""
    SELECT c.id || chr(9) || c.name || chr(9) || COALESCE(c.krisha_url,'') || chr(9)
           || COALESCE(c.source_info->'homsters'->>'url','') || chr(9)
           || COALESCE(c.source_info->'korter'->>'url','') || chr(9) || 'X'
    FROM complexes c
    WHERE c.is_newbuild AND (c.completion_year IS NULL OR c.completion_year = 0)
""").splitlines():
    if not l:
        continue
    p = l.split('\t')
    rows.append((int(p[0]), p[1], p[2], p[3], p[4]))
print(f"ЖК без года: {len(rows)}")

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0'
def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode('utf-8', 'replace')

def extract_year(text):
    # ищем "2026" в контексте сдачи
    m = re.search(r'(?:сдач[аи]|завершени[ея]|ввод[а]? в эксплуатацию|год сдачи)[^0-9]{0,60}(20\d\d)', text, re.I)
    if m:
        return int(m.group(1))
    # "IV квартал 2026" / "4 кв. 2026"
    m = re.search(r'(?:квартал|кв\.?)[^0-9]{0,20}(20\d\d)', text, re.I)
    if m:
        return int(m.group(1))
    return None

results = {}
for cid, name, krisha_url, homsters_url, korter_url in rows:
    year = None
    src = None
    # 1) Крыша
    if krisha_url:
        try:
            t = fetch(krisha_url)
            year = extract_year(t)
            if year:
                src = 'krisha'
        except Exception:
            pass
        time.sleep(1.2)
    # 2) homsters
    if not year and homsters_url:
        try:
            t = fetch(homsters_url)
            year = extract_year(t)
            if year:
                src = 'homsters'
        except Exception:
            pass
        time.sleep(1.2)
    # 3) korter
    if not year and korter_url:
        try:
            t = fetch(korter_url)
            year = extract_year(t)
            if year:
                src = 'korter'
        except Exception:
            pass
        time.sleep(1.2)
    results[cid] = (name, year, src)
    print(f"  {cid} {name[:30]:30} -> {year} ({src or 'нет'})", flush=True)

# записать найденные
upd = 0
for cid, (name, year, src) in results.items():
    if year:
        psql(f"UPDATE complexes SET completion_year = {year} WHERE id = {cid}")
        upd += 1
print(f"\nзаписано лет: {upd}")
print(psql("SELECT COUNT(*) FROM complexes WHERE is_newbuild AND (completion_year IS NULL OR completion_year = 0)"))
