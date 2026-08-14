#!/usr/bin/env python3
"""Аннотация снимка хайпа: summary, выводы + прогоны произвольных ресурсов.
Запуск: venv/bin/python hype_tracker/hype_annotate.py --snapshot-id N \
  --summary '...' [--conclusions '...'] \
  [--resources '[[\"Zakon.kz\", 5, \"заголовки...\"], ...]']  (JSON-строка) \
  [--ratings '[{\"name\": \"ЖК X\", \"rating\": 90, \"reason\": \"...\"}]']
"""
import argparse
import json
import sys
from datetime import datetime, timezone
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
    ap.add_argument("--ratings", default="")
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

    if a.ratings:
        try:
            items = json.loads(a.ratings)
        except Exception as e:
            print(f"# ratings JSON error: {e}", file=sys.stderr)
            items = []
        now = datetime.now(timezone.utc)
        for row in items:
            name = row.get("name") or row.get("location")
            if not name:
                continue
            try:
                rating = float(row.get("rating", 0))
            except (TypeError, ValueError):
                rating = 0.0
            reason = row.get("reason")
            sentiment = row.get("sentiment")
            cur.execute("SELECT id FROM hype_locations WHERE name = %s", (name,))
            r = cur.fetchone()
            if not r:
                cur.execute(
                    "INSERT INTO hype_locations (name, rating, reason, sentiment, first_seen, last_seen) "
                    "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                    (name, rating, reason, sentiment, now, now))
                lid = cur.fetchone()[0]
            else:
                lid = r[0]
                if sentiment is not None:
                    cur.execute(
                        "UPDATE hype_locations SET rating = %s, reason = %s, sentiment = %s, last_seen = %s WHERE id = %s",
                        (rating, reason, sentiment, now, lid))
                else:
                    cur.execute(
                        "UPDATE hype_locations SET rating = %s, reason = %s, last_seen = %s WHERE id = %s",
                        (rating, reason, now, lid))
            cur.execute(
                "INSERT INTO hype_location_history (location_id, ts, rating, sources, note, sentiment) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (lid, now, rating, json.dumps([f"snapshot {a.snapshot_id}"]), reason, sentiment))
    db.commit()
    db.close()


if __name__ == "__main__":
    main()
