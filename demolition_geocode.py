#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Геокодирование адресов demolition_houses через OSM Nominatim (пауза 1.1с)."""
import subprocess, time, urllib.parse, urllib.request, json

def psql(sql: str) -> str:
    r = subprocess.run(["sudo", "-u", "postgres", "psql", "-d", "krisha_bot", "-t", "-A", "-c", sql],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:300])
    return r.stdout.strip()

def geocode(addr: str):
    q = urllib.parse.quote(f"{addr}, Астана, Казахстан")
    url = f"https://nominatim.openstreetmap.org/search?q={q}&format=json&limit=1&accept-language=ru"
    req = urllib.request.Request(url, headers={"User-Agent": "hatuli-research/1.0"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        rows = json.loads(resp.read().decode())
    if rows:
        return float(rows[0]["lat"]), float(rows[0]["lon"])
    return None, None

rows = [r.split("|") for r in psql("SELECT id, address FROM demolition_houses WHERE lat IS NULL").splitlines() if r]
print(f"К геокодированию: {len(rows)}")
ok = 0
for rid, addr in rows:
    lat, lon = geocode(addr)
    if lat:
        psql(f"UPDATE demolition_houses SET lat={lat}, lon={lon}, geocoded_at=now() WHERE id={rid}")
        ok += 1
        print(f"✓ {addr} -> {lat:.5f},{lon:.5f}", flush=True)
    else:
        print(f"✗ {addr} — не найдено", flush=True)
    time.sleep(1.1)
print(f"Готово: {ok}/{len(rows)}")
