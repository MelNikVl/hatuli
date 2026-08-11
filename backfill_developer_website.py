#!/usr/bin/env python3
"""One-off backfill: developers.website from homeportal.kz API 'develpoer_website'
field (per developerData in objects-detail), for developers we've already matched
to a homeportal-registered ЖК but don't yet have a website for.
Polite: 1s delay between API calls (only ~41 calls total)."""
import json
import subprocess
import time
import urllib.request

API = "https://api.homeportal.kz/api/v1"
UA = "Mozilla/5.0 (X11; Linux x86_64) Chrome/124.0 Safari/537.36"


def psql(sql: str) -> str:
    r = subprocess.run(["sudo", "-u", "postgres", "psql", "-d", "krisha_bot", "-t", "-A", "-c", sql],
                        capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:300])
    return r.stdout.strip()


def req(url: str) -> dict:
    r = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(r, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


ESC = lambda s: str(s).replace(chr(39), chr(39) * 2) if s is not None else ""


def main():
    rows = psql("""
        SELECT d.id, MIN(h.object_id)
        FROM complexes c
        JOIN developers d ON d.id = c.developer_id
        JOIN homeportal_objects h ON h.matched_complex_id = c.id
        WHERE d.website IS NULL OR d.website = ''
        GROUP BY d.id
    """).splitlines()
    print(f"{len(rows)} developers to check")
    updated, skipped = 0, 0
    for i, line in enumerate(rows):
        if not line.strip():
            continue
        dev_id_s, obj_id_s = line.split("|")
        dev_id, obj_id = int(dev_id_s), int(obj_id_s)
        try:
            det = req(f"{API}/objects-detail/{obj_id}")
            dev = ((det.get("data") or {}).get("basicData") or {}).get("developerData") or {}
            site = (dev.get("develpoer_website") or "").strip()
            if site and site.startswith("http"):
                psql(f"UPDATE developers SET website = '{ESC(site)}', updated_at = now() "
                     f"WHERE id = {dev_id} AND (website IS NULL OR website = '')")
                print(f"[{i+1}/{len(rows)}] dev={dev_id} obj={obj_id} -> {site}")
                updated += 1
            else:
                print(f"[{i+1}/{len(rows)}] dev={dev_id} obj={obj_id} -> no website field")
                skipped += 1
        except Exception as exc:
            print(f"[{i+1}/{len(rows)}] dev={dev_id} obj={obj_id} ERROR: {exc}")
            skipped += 1
        time.sleep(1.0)
    print(f"done: updated={updated} skipped={skipped}")


if __name__ == "__main__":
    main()
