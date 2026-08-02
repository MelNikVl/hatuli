#!/usr/bin/env python3
"""Апсерт локаций хайпа из JSON (для ежедневного новостного анализа).
Формат файла: [{"name": "...", "district": "...", "lat": 51.1, "lon": 71.4,
                "rating": 80, "reason": "...", "sources": ["Zakon.kz","Threads"]}, ...]
Обновляет hype_locations (по имени) + пишет hype_location_history.
Запуск: venv/bin/python hype_tracker/location_upsert.py --file /tmp/locs.json
"""
import argparse
import json
from pathlib import Path

import psycopg2
import psycopg2.extras

BASE = Path("/home/nik/krisha_bot")


def load_database_url() -> str:
    for line in (BASE / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip()
    return "postgresql://krisha@localhost/krisha_bot"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    a = ap.parse_args()

    locs = json.loads(Path(a.file).read_text(encoding="utf-8"))
    db = psycopg2.connect(load_database_url().rsplit("/", 1)[0] + "/hype_tracker")
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    for loc in locs:
        name = (loc.get("name") or "").strip()[:120]
        if not name:
            continue
        lat = loc.get("lat")
        lon = loc.get("lon")
        rating = float(loc.get("rating") or 0)
        reason = (loc.get("reason") or "")[:600]
        district = (loc.get("district") or "")[:60] or None
        sources = json.dumps(loc.get("sources") or [], ensure_ascii=False)

        cur.execute("SELECT id FROM hype_locations WHERE name = %s", (name,))
        row = cur.fetchone()
        if row:
            lid = row["id"]
            cur.execute(
                "UPDATE hype_locations SET rating = %s, reason = %s, lat = %s, lon = %s, "
                "district = %s, last_seen = now() WHERE id = %s",
                (rating, reason, lat, lon, district, lid))
        else:
            cur.execute(
                "INSERT INTO hype_locations (name, district, lat, lon, rating, reason) "
                "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                (name, district, lat, lon, rating, reason))
            lid = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO hype_location_history (location_id, ts, rating, sources, note) "
            "VALUES (%s, now(), %s, %s::jsonb, %s)",
            (lid, rating, sources, reason or None))

    db.commit()
    db.close()
    print(f"локаций обновлено: {len(locs)}")


if __name__ == "__main__":
    main()
