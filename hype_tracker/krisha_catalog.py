#!/usr/bin/env python3
"""Коллектор каталога ЖК Крыши (Астана): слаги + названия, пауза 4с.
Каталог ~8 страниц по ~190 ЖК. Сохраняет в krisha_complex_catalog."""
import re
import subprocess
import sys
import time
from urllib.request import Request, urlopen

UA = "Mozilla/5.0 (X11; Linux x86_64) Chrome/124.0 Safari/537.36"
DELAY = 4.0
BASE = "https://krisha.kz/complex/search/astana/"


def psql(sql: str) -> str:
    r = subprocess.run(["sudo", "-u", "postgres", "psql", "-d", "krisha_bot", "-t", "-A", "-c", sql],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:300])
    return r.stdout.strip()


def fetch(url: str) -> str:
    return urlopen(Request(url, headers={"User-Agent": UA}), timeout=30).read().decode("utf-8", "ignore")


def main() -> int:
    psql("""CREATE TABLE IF NOT EXISTS krisha_complex_catalog (
        slug TEXT PRIMARY KEY, name TEXT, url TEXT, found_at TIMESTAMPTZ DEFAULT now())""")
    seen = set()
    new = 0
    # страницы 1..8 (дальше каталог зацикливается)
    for page in range(1, 9):
        url = BASE if page == 1 else f"{BASE}?page={page}"
        try:
            html = fetch(url)
        except Exception as e:
            print(f"❌ page {page}: {e}")
            continue
        pairs = re.findall(r'href="(/complex/show/astana/([a-z0-9-]+)/)"[^>]*>\s*([^<]{2,90})<', html)
        for href, slug, name in pairs:
            name = re.sub(r"\s+", " ", name).strip()
            if not name or slug in seen:
                continue
            seen.add(slug)
            esc = name.replace(chr(39), chr(39) * 2)
            try:
                psql(f"INSERT INTO krisha_complex_catalog (slug, name, url) VALUES "
                     f"('{slug}', '{esc}', 'https://krisha.kz{chr(47)}complex{chr(47)}show{chr(47)}astana{chr(47)}{slug}{chr(47)}') "
                     f"ON CONFLICT (slug) DO NOTHING")
                new += 1
            except Exception as e:
                print(f"  insert {slug}: {e}")
        print(f"page {page}: уникальных {len(seen)} (+{new})")
        time.sleep(DELAY)

    total = psql("SELECT count(*) FROM krisha_complex_catalog")
    print(f"\nитог: в каталоге {total} ЖК")
    return 0


if __name__ == "__main__":
    sys.exit(main())
