#!/usr/bin/env python3
"""Парсим sitemap complexes.xml Крыши: все слаги ЖК → krisha_complex_catalog."""
import re
import subprocess
import sys
from urllib.request import Request, urlopen

UA = "Mozilla/5.0 (X11; Linux x86_64) Chrome/124.0 Safari/537.36"
SITEMAP = "https://krisha.kz/sitemap/frontend/complexes.xml"


def psql(sql: str) -> str:
    r = subprocess.run(["sudo", "-u", "postgres", "psql", "-d", "krisha_bot", "-t", "-A", "-c", sql],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:300])
    return r.stdout.strip()


def main() -> int:
    psql("""CREATE TABLE IF NOT EXISTS krisha_complex_catalog (
        slug TEXT PRIMARY KEY, name TEXT, url TEXT, found_at TIMESTAMPTZ DEFAULT now())""")
    xml = urlopen(Request(SITEMAP, headers={"User-Agent": UA}), timeout=60).read().decode("utf-8", "ignore")
    locs = re.findall(r"<loc>(https://krisha\.kz/complex/show/([a-z-]+)/([a-z0-9-]+)/)</loc>", xml)
    print(f"всего в sitemap: {len(locs)}")

    astana = [l for l in locs if l[1] in ("astana", "nur-sultan")]
    print(f"Астана (astana+nur-sultan): {len(astana)}")
    new = 0
    for url, region, slug in astana:
        try:
            psql(f"INSERT INTO krisha_complex_catalog (slug, url) VALUES "
                 f"('{slug}', 'https://krisha.kz/complex/show/{region}/{slug}/') "
                 f"ON CONFLICT (slug) DO NOTHING")
            new += 1
        except Exception as e:
            print(f"  {slug}: {e}")
    total = psql("SELECT count(*) FROM krisha_complex_catalog")
    print(f"добавлено: {new}, всего в каталоге: {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
