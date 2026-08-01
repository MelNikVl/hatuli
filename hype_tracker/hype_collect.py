#!/usr/bin/env python3
"""Коллектор хайп-снимков (2 раза/день): noz.kz + krisha.kz + внутренние метрики.
Пишет снимок в hype_snapshots и прогоны ресурсов в hype_resource_runs.
Запуск: venv/bin/python hype_tracker/hype_collect.py --period morning|evening
"""
import argparse
import json
import re
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import psycopg2
import psycopg2.extras
from bs4 import BeautifulSoup

BASE = Path("/home/nik/krisha_bot")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"

KNOWN_RESOURCES = [
    ("noz.kz рейтинг новостроек", "https://noz.kz/novostroyki/astana/rating/2026/", "aggregator"),
    ("krisha.kz новостройки (популярные)", "https://krisha.kz/complex/", "aggregator"),
    ("korter.kz ЖК", "https://korter.kz", "aggregator"),
    ("kn.kz", "https://www.kn.kz", "market"),
    ("etagi.com Астана", "https://astana.etagi.com", "market"),
    ("homsters.kz ЖК", "https://homsters.kz", "market"),
    ("внутренняя база krisha_bot", "", "internal"),
    ("Kazinform", "https://www.inform.kz", "news"),
    ("Zakon.kz", "https://www.zakon.kz", "news"),
    ("Liter.kz", "https://liter.kz", "news"),
    ("inastana.kz", "https://www.inastana.kz", "news"),
    ("Azattyq Ruhy (недвижимость)", "https://rus.azattyq-ruhy.kz", "news"),
    ("Capital Realty блог", "https://capital-realty.kz", "blog"),
    ("SAT-NS блог", "https://sat-ns.kz", "blog"),
    ("na-tumbe.kz", "https://na-tumbe.kz", "blog"),
    ("Instagram-хештеги", "https://www.instagram.com", "social"),
    ("Threads", "https://www.threads.net", "social"),
    ("TikTok", "https://www.tiktok.com", "social"),
    ("YouTube-обзоры риелторов", "https://www.youtube.com", "social"),
    ("2GIS-отзывы", "https://2gis.kz", "social"),
    ("last30days-движок", "", "social"),
    ("ЕГКН (кадастр)", "https://egkn.kz", "official"),
    ("Госэкспертиза", "https://expertiza.kz", "official"),
    ("Акимат Астаны (планы)", "https://www.gov.kz/memleket/entities/astana", "official"),
]


def load_database_url() -> str:
    for line in (BASE / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip()
    return "postgresql://krisha@localhost/krisha_bot"


def conn(db: str):
    return psycopg2.connect(load_database_url().rsplit("/", 1)[0] + "/" + db)


def fetch(url: str, timeout: int = 25) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def seed_resources(cur) -> None:
    for name, url, rtype in KNOWN_RESOURCES:
        cur.execute("SELECT id FROM hype_resources WHERE name = %s", (name,))
        if not cur.fetchone():
            cur.execute("INSERT INTO hype_resources (name, url, rtype) VALUES (%s, %s, %s)",
                        (name, url, rtype))


def parse_noz(html: str) -> list[str]:
    """Рейтинг новостроек noz.kz по просмотрам: имена ЖК из таблицы."""
    soup = BeautifulSoup(html, "html.parser")
    names = []
    for a in soup.select("a[href*='/novostroyki/']"):
        name = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
        if name and len(name) > 2 and not any(k in name.lower() for k in ("алматы", "астана", "новостройк")):
            if name not in names:
                names.append(name)
    return names[:15]


def parse_krisha(html: str) -> list[str]:
    """Список ЖК krisha.kz/complex: имена из заголовков карточек."""
    soup = BeautifulSoup(html, "html.parser")
    names = []
    for a in soup.select("a[href*='/complex/']"):
        name = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
        if name and len(name) > 2 and name not in names:
            names.append(name)
    return names[:200]


def collect_internal(cur) -> dict:
    """Внутренние метрики krisha_bot."""
    cur.execute("SELECT count(*) FROM complexes")
    complexes = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM listings WHERE status = 'active'")
    listings = cur.fetchone()[0]
    return {"complexes": complexes, "listings": listings}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", choices=["morning", "evening"], default="morning")
    a = ap.parse_args()

    db = conn("hype_tracker")
    cur = db.cursor()
    seed_resources(cur)

    results = {}

    # noz.kz
    try:
        items = parse_noz(fetch("https://noz.kz/novostroyki/astana/rating/2026/"))
        results["noz.kz рейтинг новостроек"] = (len(items), items[:10])
        time.sleep(1)
    except Exception as e:
        results["noz.kz рейтинг новостроек"] = (0, f"error: {e}")

    # krisha.kz
    try:
        items = parse_krisha(fetch("https://krisha.kz/complex/"))
        results["krisha.kz новостройки (популярные)"] = (len(items), items[:10])
        time.sleep(1)
    except Exception as e:
        results["krisha.kz новостройки (популярные)"] = (0, f"error: {e}")

    # внутренние метрики
    try:
        db2 = conn("krisha_bot")
        cur2 = db2.cursor()
        metrics = collect_internal(cur2)
        db2.close()
        results["внутренняя база krisha_bot"] = (metrics["listings"], [f"ЖК: {metrics['complexes']}", f"объявлений: {metrics['listings']}"])
    except Exception as e:
        results["внутренняя база krisha_bot"] = (0, f"error: {e}")

    # снимок
    cur.execute(
        "INSERT INTO hype_snapshots (period, summary) VALUES (%s, %s) RETURNING id",
        (a.period, json.dumps({"resources": {k: v[0] for k, v in results.items()}},
                              ensure_ascii=False)))
    sid = cur.fetchone()[0]
    for name, (n, notes) in results.items():
        cur.execute("SELECT id FROM hype_resources WHERE name = %s", (name,))
        rid = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO hype_resource_runs (snapshot_id, resource_id, items_found, status, notes) "
            "VALUES (%s, %s, %s, 'ok', %s)",
            (sid, rid, n, json.dumps(notes, ensure_ascii=False)[:500] if notes else None))
    db.commit()
    db.close()
    print(f"снимок #{sid} ({a.period}): " +
          ", ".join(f"{k}={v[0]}" for k, v in results.items()))


if __name__ == "__main__":
    main()
