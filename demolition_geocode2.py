#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Добиваем геокод: варианты адреса (с/без префикса, каз. транслит)."""
import subprocess, time, urllib.parse, urllib.request, json

def psql(sql: str) -> str:
    r = subprocess.run(["sudo", "-u", "postgres", "psql", "-d", "krisha_bot", "-t", "-A", "-c", sql],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:300])
    return r.stdout.strip()

def geocode(q: str):
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"q": q, "format": "json", "limit": 1, "accept-language": "ru"})
    req = urllib.request.Request(url, headers={"User-Agent": "hatuli-research/1.0"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        rows = json.loads(resp.read().decode())
    return (float(rows[0]["lat"]), float(rows[0]["lon"])) if rows else (None, None)

rows = [r.split("|") for r in psql("SELECT id, address FROM demolition_houses WHERE lat IS NULL ORDER BY id").splitlines() if r]
print(f"Осталось: {len(rows)}")
ok = 0
for rid, addr in rows:
    base = addr.replace("пр. ", "").replace("ул. ", "").replace("пер. ", "")
    variants = [
        f"{base}, Астана, Казахстан",
        f"Астана, {base}",
        f"{addr}, Астана",
    ]
    lat = lon = None
    for v in variants:
        lat, lon = geocode(v)
        if lat:
            break
        time.sleep(0.4)
    if lat:
        psql(f"UPDATE demolition_houses SET lat={lat}, lon={lon}, geocoded_at=now() WHERE id={rid}")
        ok += 1
        print(f"✓ {addr} -> {lat:.5f},{lon:.5f}", flush=True)
    else:
        print(f"✗ {addr}", flush=True)
    time.sleep(1.0)
print(f"Итог: {ok}/{len(rows)}")
