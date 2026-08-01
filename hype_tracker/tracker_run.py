#!/usr/bin/env python3
"""Запись прогона ресурса в hype_tracker без снимка (snapshot_id=NULL) —
для высокочастотных сканов: Threads каждый час, СМИ 5 раз/день.

Запуск: venv/bin/python hype_tracker/tracker_run.py --resource NAME --items N [--notes '...']
"""
import argparse
import sys
from pathlib import Path

import psycopg2

BASE = Path("/home/nik/krisha_bot")


def load_database_url() -> str:
    for line in (BASE / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip()
    return "postgresql://krisha@localhost/krisha_bot"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--resource", required=True)
    ap.add_argument("--items", type=int, default=0)
    ap.add_argument("--notes", default="")
    a = ap.parse_args()

    url = load_database_url().rsplit("/", 1)[0] + "/hype_tracker"
    db = psycopg2.connect(url)
    cur = db.cursor()
    cur.execute("SELECT id FROM hype_resources WHERE name = %s", (a.resource,))
    row = cur.fetchone()
    if not row:
        cur.execute("INSERT INTO hype_resources (name, rtype) VALUES (%s, 'manual') RETURNING id", (a.resource,))
        rid = cur.fetchone()[0]
    else:
        rid = row[0]
    cur.execute(
        "INSERT INTO hype_resource_runs (snapshot_id, resource_id, items_found, status, notes) "
        "VALUES (NULL, %s, %s, 'ok', %s)",
        (rid, a.items, a.notes or None))
    db.commit()
    db.close()


if __name__ == "__main__":
    main()
