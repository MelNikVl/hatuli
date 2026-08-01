#!/usr/bin/env python3
"""Скан СМИ (5 раз/день): Google News RSS по недвижимости Астаны.
Считает материалы по доменам и пишет прогон каждого СМИ-ресурса в hype_tracker.
Молчит при успехе (для no_agent крона)."""
import re
import sys
import time
import urllib.request
import urllib.parse
from pathlib import Path

import psycopg2

BASE = Path("/home/nik/krisha_bot")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"

# домен -> имя ресурса в hype_resources
DOMAIN_MAP = {
    "zakon.kz": "Zakon.kz",
    "inform.kz": "Kazinform",
    "liter.kz": "Liter.kz",
    "inastana.kz": "inastana.kz",
    "azattyq-ruhy.kz": "Azattyq Ruhy (недвижимость)",
    "24.kz": "24.kz",
    "nur.kz": "NUR.KZ",
    "krisha.kz": "krisha.kz новостройки (популярные)",
    "korter.kz": "korter.kz",
    "homsters.kz": "homsters.kz",
}
QUERIES = [
    "Астана недвижимость",
    "Астана новостройки",
    "ипотека Казахстан жильё",
    "рынок недвижимости Казахстан",
]


def load_database_url() -> str:
    for line in (BASE / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip()
    return "postgresql://krisha@localhost/krisha_bot"


def get_rss(q: str) -> str:
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": q, "hl": "ru", "gl": "KZ", "ceid": "KZ:ru"})
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read().decode("utf-8", errors="replace")


def strip_tags(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s)).strip()


def parse_rss(xml: str) -> list[tuple[str, str]]:
    """(домен, заголовок)"""
    out = []
    for it in re.findall(r"<item>(.*?)</item>", xml, re.S):
        tm = re.search(r"<title>(.*?)</title>", it, re.S)
        sm = re.search(r"<source url=\"[^\"]+\">(.*?)</source>", it, re.S)
        if not tm:
            continue
        title = strip_tags(tm.group(1))
        if len(title) < 20:
            continue
        dom = (sm.group(1) if sm else "").strip().lower()
        out.append((dom, title))
    return out


def main() -> None:
    db = psycopg2.connect(load_database_url().rsplit("/", 1)[0] + "/hype_tracker")
    cur = db.cursor()

    counts: dict[str, int] = {}
    titles: dict[str, list[str]] = {}
    for q in QUERIES:
        try:
            for dom, title in parse_rss(get_rss(q)):
                name = DOMAIN_MAP.get(dom)
                if not name:
                    continue
                counts[name] = counts.get(name, 0) + 1
                titles.setdefault(name, []).append(title[:160])
            time.sleep(2)
        except Exception as e:
            print(f"# rss error {q}: {e}", file=sys.stderr)

    for name, n in counts.items():
        cur.execute("SELECT id FROM hype_resources WHERE name = %s", (name,))
        row = cur.fetchone()
        if not row:
            cur.execute("INSERT INTO hype_resources (name, rtype) VALUES (%s, 'news') RETURNING id", (name,))
            rid = cur.fetchone()[0]
        else:
            rid = row[0]
        notes = " | ".join(titles.get(name, [])[:3])[:400] or None
        cur.execute(
            "INSERT INTO hype_resource_runs (snapshot_id, resource_id, items_found, status, notes) "
            "VALUES (NULL, %s, %s, 'ok', %s)", (rid, n, notes))
    db.commit()
    db.close()
    # тихо


if __name__ == "__main__":
    main()
