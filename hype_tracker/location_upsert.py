#!/usr/bin/env python3
"""Апсерт локаций хайпа из JSON (для ручного/ежедневного новостного анализа).
Формат файла: [{"name": "...", "district": "...", "lat": 51.1, "lon": 71.4,
                "rating": 80, "reason": "...", "sources": ["Zakon.kz","Threads"]}, ...]
Обновляет hype_locations (по имени) + пишет hype_location_history.
Запуск: venv/bin/python hype_tracker/location_upsert.py --file /tmp/locs.json

upsert_location() вынесена отдельно, чтобы её мог дергать напрямую
news_analyze.py (автоматический пайплайн) — одна логика апсерта на двоих,
без дублирования SQL.
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


def upsert_location(cur, loc: dict) -> int | None:
    """Апсерт одной локации хайпа по имени + запись в hype_location_history.
    cur — курсор psycopg2 (RealDictCursor или обычный), открытый на БД hype_tracker.
    Возвращает id локации, либо None если name пустой.

    ВАЖНО: этот апсерт дёргают НЕСКОЛЬКО независимых пайплайнов на один и тот
    же ЖК в один день (наш news_analyze.py по расписанию 06:37 + внешний
    ежедневный новостной разбор ~21:00) — раньше был чистый last-write-wins:
    более поздний прогон мог тихо ПОНИЗИТЬ рейтинг, поставленный более ранним
    (наблюдалось: «тан» 30↔40, «family town»/«Sensata» задвоенные записи в
    истории с одинаковым рейтингом). Правило теперь: если прошлое обновление
    было < 24ч назад — берём max(старый, новый) рейтинг вместо слепой
    перезаписи; reason обновляем только если новый рейтинг реально победил.
    Если прошлое обновление старше суток — новый день, просто заменяем."""
    name = (loc.get("name") or "").strip()[:120]
    if not name:
        return None
    lat = loc.get("lat")
    lon = loc.get("lon")
    rating = float(loc.get("rating") or 0)
    reason = (loc.get("reason") or "")[:600]
    district = (loc.get("district") or "")[:60] or None
    sources = json.dumps(loc.get("sources") or [], ensure_ascii=False)

    cur.execute("SELECT id, rating, reason, last_seen FROM hype_locations WHERE name = %s", (name,))
    row = cur.fetchone()
    if row:
        lid = row["id"] if isinstance(row, dict) else row[0]
        prev_rating = float((row["rating"] if isinstance(row, dict) else row[1]) or 0)
        prev_reason = (row["reason"] if isinstance(row, dict) else row[2]) or ""
        prev_last_seen = row["last_seen"] if isinstance(row, dict) else row[3]

        is_fresh = False
        if prev_last_seen:
            cur.execute("SELECT (now() - %s) < interval '24 hours' AS fresh", (prev_last_seen,))
            fresh_row = cur.fetchone()
            is_fresh = bool(fresh_row["fresh"] if isinstance(fresh_row, dict) else fresh_row[0])

        if is_fresh and prev_rating > rating:
            rating, reason = prev_rating, prev_reason

        cur.execute(
            "UPDATE hype_locations SET rating = %s, reason = %s, lat = %s, lon = %s, "
            "district = %s, last_seen = now() WHERE id = %s",
            (rating, reason, lat, lon, district, lid))

        # Не плодим шумные дубли в истории, если за последние 3ч уже
        # записан ровно такой же рейтинг для этой локации (неважно, каким
        # из пайплайнов).
        cur.execute(
            "SELECT 1 FROM hype_location_history WHERE location_id = %s "
            "AND rating = %s AND ts > now() - interval '3 hours' LIMIT 1",
            (lid, rating))
        if cur.fetchone():
            return lid
    else:
        cur.execute(
            "INSERT INTO hype_locations (name, district, lat, lon, rating, reason) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (name, district, lat, lon, rating, reason))
        r = cur.fetchone()
        lid = r["id"] if isinstance(r, dict) else r[0]
    cur.execute(
        "INSERT INTO hype_location_history (location_id, ts, rating, sources, note) "
        "VALUES (%s, now(), %s, %s::jsonb, %s)",
        (lid, rating, sources, reason or None))
    return lid


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    a = ap.parse_args()

    locs = json.loads(Path(a.file).read_text(encoding="utf-8"))
    db = psycopg2.connect(load_database_url().rsplit("/", 1)[0] + "/hype_tracker")
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    n = 0
    for loc in locs:
        if upsert_location(cur, loc) is not None:
            n += 1

    db.commit()
    db.close()
    print(f"локаций обновлено: {n}")


if __name__ == "__main__":
    main()
