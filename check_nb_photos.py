#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проверка фото ЖК-новостроек: битые homeportal vs рабочие."""
import subprocess, json, sys, time
sys.path.insert(0, '/home/nik/krisha_bot')

def psql(sql):
    r = subprocess.run(['sudo', '-u', 'postgres', 'psql', '-d', 'krisha_bot', '-t', '-A',
                        '-F', chr(9), '-c', sql], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:300])
    return r.stdout.strip()

# новостройки с фото
rows = []
for l in psql("""
    SELECT c.id || chr(9) || c.name || chr(9) || COALESCE(c.photos::text, '[]') || chr(9) || 'X'
    FROM complexes c WHERE c.is_newbuild AND c.photos IS NOT NULL AND jsonb_array_length(c.photos) > 0
""").splitlines():
    if not l:
        continue
    p = l.split('\t')
    try:
        ph = json.loads(p[2])
    except Exception:
        ph = []
    rows.append((int(p[0]), p[1], ph))

print(f"новостроек с фото: {len(rows)}", flush=True)
hp_bad = 0
krisha_ok = 0
samples = []
for cid, name, ph in rows:
    hp = [u for u in ph if 'homeportal' in u]
    kr = [u for u in ph if 'krisha' in u or 'kcdn' in u]
    if hp and not kr:
        hp_bad += 1
        if len(samples) < 8:
            samples.append((cid, name, hp[0][:60]))
    if kr:
        krisha_ok += 1

print(f"только homeportal (вероятно битые): {hp_bad}")
print(f"есть krisha/kcdn фото: {krisha_ok}")
print("примеры homeportal-only:")
for cid, name, url in samples:
    print(f"  {cid} | {name[:35]:35} | {url}")

# также: сколько юнитов без layout_photo_url
print("", flush=True)
print(psql("SELECT COUNT(*) FILTER (WHERE layout_photo_url IS NOT NULL) || '/' || COUNT(*) FROM newbuild_units"))
