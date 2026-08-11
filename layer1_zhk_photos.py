#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Слой 1: фото ЖК -> объявления без фото (активные, с complex_name)."""
import subprocess, json

def psql(sql):
    r = subprocess.run(['sudo', '-u', 'postgres', 'psql', '-d', 'krisha_bot', '-t', '-A', '-c', sql],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:300])
    return r.stdout.strip()

# собрать фото ЖК: name -> [urls]
rows = psql("""
    SELECT c.name || chr(9) || COALESCE(jsonb_array_length(c.photos::jsonb), 0)::text || chr(9) || COALESCE(c.photos::text,'') || chr(9) || COALESCE(c.photo_url,'') || chr(9) || 'X'
    FROM complexes c
    WHERE c.is_garbage IS NOT TRUE
      AND (c.photos IS NOT NULL AND jsonb_array_length(c.photos::jsonb) > 0 OR c.photo_url IS NOT NULL)
""")
cx_photos = {}
for l in rows.splitlines():
    if not l:
        continue
    p = l.split('\t')
    name = p[0].strip().lower()
    urls = []
    if p[2] and p[2] != 'null':
        try:
            urls = json.loads(p[2])
        except Exception:
            urls = []
    if p[3]:
        urls.insert(0, p[3])
    urls = [u for u in urls if u]
    if urls:
        cx_photos[name] = urls
print(f"ЖК с фото: {len(cx_photos)}", flush=True)

# объявления: активные, с ЖК, без фото
al = psql("""
    SELECT id || chr(9) || lower(trim(complex_name)) || chr(9) || 'X'
    FROM apartment_listings
    WHERE is_active IS NOT FALSE AND COALESCE(is_duplicate, FALSE) = FALSE
      AND complex_name IS NOT NULL AND complex_name != ''
      AND (photos IS NULL OR jsonb_array_length(photos) = 0)
""")
upd = 0
batch = []
for l in al.splitlines():
    if not l:
        continue
    p = l.split('\t')
    lid, cx = p[0], p[1]
    if cx in cx_photos:
        arr = json.dumps(cx_photos[cx][:10], ensure_ascii=False).replace("'", "''")
        batch.append((lid, arr))
        if len(batch) >= 50:
            vals = ','.join(f"('{a}', '{b}'::jsonb)" for a, b in batch)
            psql(f"UPDATE apartment_listings SET photos = v.ph FROM (VALUES {vals}) AS v(id, ph) WHERE apartment_listings.id = v.id")
            upd += len(batch)
            batch = []
if batch:
    vals = ','.join(f"('{a}', '{b}'::jsonb)" for a, b in batch)
    psql(f"UPDATE apartment_listings SET photos = v.ph FROM (VALUES {vals}) AS v(id, ph) WHERE apartment_listings.id = v.id")
    upd += len(batch)

print(f"объявлений получили фото ЖК: {upd}", flush=True)
print(psql("""
    SELECT COUNT(*) FILTER (WHERE photos IS NOT NULL AND jsonb_array_length(photos) > 0) || '/' || COUNT(*) 
    FROM apartment_listings WHERE is_active IS NOT FALSE AND COALESCE(is_duplicate, FALSE) = FALSE AND complex_name IS NOT NULL
"""), flush=True)
