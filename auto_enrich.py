#!/usr/bin/env python3
"""Autonomous enrichment: search + update complexes on the server"""
import psycopg2, re, json, sys, time
from ddgs import DDGS

BATCH_SIZE = 15
SLEEP_BETWEEN = 0.8
sys.stdout.reconfigure(encoding='utf-8')

# Connect to DB
with open('/home/nik/krisha_bot/.env') as f:
    env = f.read()
m = re.search(r'postgresql://(.+):(.+)@(.+)/(.+)', env)
conn = psycopg2.connect(host=m.group(3), dbname=m.group(4), user=m.group(1), password=m.group(2))
cur = conn.cursor()

def classify(text):
    t = text.lower()
    if any(w in t for w in ['элит', 'premium', 'премиум', 'elite']): return 'элит'
    if any(w in t for w in ['бизнес', 'business']): return 'бизнес'
    if any(w in t for w in ['комфорт', 'comfort', 'комфорт+']): return 'комфорт'
    if any(w in t for w in ['эконом', 'econom']): return 'эконом'
    return None

def extract_year(text):
    yrs = re.findall(r'(?:^|\s)(20[0-2]\d|19[89]\d)(?:\s|$|\.|,|г)', text)
    if yrs:
        years = [int(y) for y in yrs if 1980 <= int(y) <= 2030]
        if years: return max(years)
    return None

def search(query):
    try:
        with DDGS() as ddgs:
            return list(ddgs.text(query, max_results=5))
    except:
        return []

batch_num = 0
total_stmts = 0

while True:
    batch_num += 1
    
    # Get complexes still missing data
    cur.execute("""
        SELECT c.id, c.name, 
               c.housing_class IS NOT NULL AND c.housing_class != '' as has_class,
               c.year_built IS NOT NULL AND c.year_built > 0 as has_year,
               c.notes IS NOT NULL AND c.notes != '' as has_notes,
               COALESCE(d.name, '') as dev_name
        FROM complexes c
        LEFT JOIN developers d ON c.developer_id = d.id
        WHERE (c.housing_class IS NULL OR c.housing_class = ''
               OR c.year_built IS NULL OR c.year_built = 0
               OR c.notes IS NULL OR c.notes = '')
        ORDER BY c.listings_count DESC NULLS LAST, c.id
        LIMIT """ + str(BATCH_SIZE))
    
    rows = cur.fetchall()
    if not rows:
        print(f'\n=== ALL DONE! {total_stmts} statements applied ===')
        break
    
    batch_stmts = 0
    print(f'\n=== Batch #{batch_num} ({len(rows)} items) ===')
    
    for r in rows:
        cid, cname, has_class, has_year, has_notes, dev_name = r
        cname_clean = cname.replace('\u0000', '')
        
        if has_class and has_year and has_notes:
            print(f'  SKIP {cid}: {cname_clean[:30]} (already complete)')
            continue
        
        query = f'{cname_clean} {dev_name} Астана жилой комплекс'
        results = search(query)
        
        housing_class = None
        year_built = None
        description = None
        
        for res in results:
            text = (res.get('body', '') + ' ' + res.get('title', '') + ' ' + res.get('href', ''))
            if not housing_class:
                housing_class = classify(text)
            if not year_built:
                year_built = extract_year(text)
            if not description and len(res.get('body', '')) > 60:
                description = res['body'][:500]
        
        sets = []
        params = []
        if not has_class and housing_class:
            sets.append('housing_class = %s')
            params.append(housing_class)
        if not has_year and year_built:
            sets.append('year_built = %s')
            params.append(year_built)
        if not has_notes and description:
            sets.append('notes = %s')
            params.append(description)
        
        if sets:
            params.append(cid)
            try:
                cur.execute(f'UPDATE complexes SET {", ".join(sets)} WHERE id = %s', params)
                conn.commit()
                batch_stmts += 1
                print(f'  OK {cid}: {cname_clean[:25]} → class={housing_class or "—"} year={year_built or "—"} desc={"✅" if description else "—"}')
            except Exception as e:
                conn.rollback()
                print(f'  ERR {cid}: {str(e)[:60]}')
        else:
            print(f'  —  {cid}: {cname_clean[:25]} → no new data')
        
        time.sleep(SLEEP_BETWEEN)
    
    total_stmts += batch_stmts
    print(f'  Batch done: {batch_stmts} stmts (total: {total_stmts})')
    time.sleep(0.5)

conn.close()
