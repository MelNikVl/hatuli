#!/usr/bin/env python3
"""Аннотация снимка хайпа: summary, выводы + прогоны произвольных ресурсов.
Запуск: venv/bin/python hype_tracker/hype_annotate.py --snapshot-id N \
  --summary '...' [--conclusions '...'] \
  [--resources '[["Zakon.kz", 5, "заголовки..."], ...]']  (JSON-строка)
"""
import argparse
import json
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
    ap.add_argument("--snapshot-id", type=int, required=True)
    ap.add_argument("--summary", default="")
    ap.add_argument("--conclusions", default="")
    ap.add_argument("--resources", default="")
    a = ap.parse_args()

    db = psycopg2.connect(load_database_url().rsplit("/", 1)[0] + "/hype_tracker")
    cur = db.cursor()

    if a.summary or a.conclusions:
        cur.execute(
            "UPDATE hype_snapshots SET summary = %s, conclusions = %s WHERE id = %s",
            (a.summary or None, a.conclusions or None, a.snapshot_id))

    if a.resources:
        try:
            items = json.loads(a.resources)
        except Exception as e:
            print(f"# resources JSON error: {e}", file=sys.stderr)
            items = []
        for row in items:
            if len(row) < 2:
                continue
            name, n = row[0], int(row[1])
            notes = row[2] if len(row) > 2 else None
            cur.execute("SELECT id FROM hype_resources WHERE name = %s", (name,))
            r = cur.fetchone()
            if not r:
                cur.execute("INSERT INTO hype_resources (name, rtype) VALUES (%s, 'news') RETURNING id", (name,))
                rid = cur.fetchone()[0]
            else:
                rid = r[0]
            cur.execute(
                "INSERT INTO hype_resource_runs (snapshot_id, resource_id, items_found, status, notes) "
                "VALUES (%s, %s, %s, 'ok', %s)",
                (a.snapshot_id, rid, n, (notes or None)))
    db.commit()
    db.close()


if __name__ == "__main__":
    main()
