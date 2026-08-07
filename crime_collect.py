#!/usr/bin/env python3
"""Сбор данных о преступности Астаны с krisha.kz/ms/geodata/crime — тот же
эндпоинт, что у кнопки "Преступность" на их /map/. Только локация + тип
преступления (crime_title, hard_code — категория 0-4, вероятно тяжесть),
без адресов/деталей — не нужны для тепловой карты, см. задачу.

Пагинация: API отдаёт максимум 100 записей за запрос, отсортированных по
возрастанию даты начиная с ?from=DD.MM.YYYY. Идём окнами: берём 100 записей,
следующий from = дата последней записи в ответе (+ сама дата переоткрывается
на случай, если в этот день записей больше 100 — но это редкость, дедуп по
natural key всё равно не даст задвоить). Останавливаемся, когда ответ
пустой ИЛИ короче 100 (значит дошли до самых свежих записей).

Запуск:
  venv/bin/python3 crime_collect.py                  # с 01.01.2024 (вся история)
  venv/bin/python3 crime_collect.py --from 01.07.2026 # только начиная с даты (докачка)
"""
from __future__ import annotations

import argparse
import sys
import time
import urllib.parse
import urllib.request
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import psycopg2
import psycopg2.extras

BASE = Path("/home/nik/krisha_bot")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
# bbox Астаны (тот же, что у остальных слоёв проекта — transport_hexes,
# population-hexes и т.п.), формат API: lon1,lat1,lon2,lat2
BOUNDS = "71.10,51.00,71.80,51.35"
API_URL = "https://krisha.kz/ms/geodata/crime"
PAGE_LIMIT = 100


def load_database_url() -> str:
    for line in (BASE / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip()
    return "postgresql://krisha@localhost/krisha_bot"


def conn():
    return psycopg2.connect(load_database_url())


def fetch_page(from_date: str) -> list[dict]:
    qs = urllib.parse.urlencode({
        "bounds": BOUNDS, "limit": str(PAGE_LIMIT), "from": from_date,
        "fields": "crime_title,hard_code,date_excitation",
    })
    req = urllib.request.Request(f"{API_URL}?{qs}", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8"))


def parse_ru_date(s: str) -> date | None:
    try:
        return datetime.strptime(s, "%d.%m.%Y").date()
    except (ValueError, TypeError):
        return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="from_date", default="01.01.2024",
                     help="откуда качать, DD.MM.YYYY (по умолчанию — вся доступная история)")
    ap.add_argument("--max-pages", type=int, default=400,
                     help="страховка от бесконечного цикла при сбое пагинации")
    a = ap.parse_args()

    db = conn()
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    from_date = a.from_date
    total_seen = 0
    total_inserted = 0
    today = date.today()
    page = 0
    while page < a.max_pages:
        page += 1
        try:
            items = fetch_page(from_date)
        except Exception as e:
            print(f"# ошибка запроса (from={from_date}): {e}", file=sys.stderr)
            break
        if not items:
            print(f"пусто на from={from_date} — похоже, дошли до конца")
            break
        total_seen += len(items)

        rows = []
        max_d = None
        for it in items:
            loc = it.get("location") or {}
            lat, lon = loc.get("lat"), loc.get("lon")
            if lat is None or lon is None:
                continue
            d = parse_ru_date(it.get("date_excitation"))
            if d and (max_d is None or d > max_d):
                max_d = d
            rows.append((lat, lon, it.get("crime_title"), it.get("hard_code"), d))

        page_inserted = 0
        for lat, lon, title, hard_code, d in rows:
            try:
                cur.execute(
                    "INSERT INTO crime_incidents (lat, lon, crime_title, hard_code, date_excitation) "
                    "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (lat, lon, date_excitation, crime_title) DO NOTHING",
                    (lat, lon, title, int(hard_code) if hard_code is not None else None, d))
                if cur.rowcount:
                    page_inserted += 1
            except Exception as e:
                print(f"# insert error: {e}", file=sys.stderr)
        total_inserted += page_inserted
        db.commit()

        print(f"страница {page}: from={from_date} -> {len(items)} записей, "
              f"новых на странице={page_inserted}, всего новых={total_inserted}")

        if len(items) < PAGE_LIMIT:
            print("страница неполная — это самые свежие доступные записи, стоп")
            break
        if max_d is None:
            print("# не смог распарсить дату последней записи — стоп во избежание зацикливания", file=sys.stderr)
            break
        if max_d >= today:
            print("дошли до сегодняшней даты — стоп")
            break
        next_from = max_d.strftime("%d.%m.%Y")
        if next_from == from_date:
            # На одну дату пришлось >=100 записей — сдвигаем на день вперёд
            # вручную, иначе from не меняется и цикл зависает на месте.
            next_from = (max_d + timedelta(days=1)).strftime("%d.%m.%Y")
        from_date = next_from
        time.sleep(1.5)  # вежливость к API — тот же принцип, что у Overpass/RSS в проекте

    cur.execute("SELECT COUNT(*) AS c FROM crime_incidents")
    total = cur.fetchone()["c"]
    db.close()
    print(f"готово: страниц={page}, записей увидено={total_seen}, новых вставлено={total_inserted}, "
          f"всего в таблице={total}")


if __name__ == "__main__":
    main()
