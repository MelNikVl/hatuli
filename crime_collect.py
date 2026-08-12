#!/usr/bin/env python3
"""Сбор данных о преступности Астаны с официального ГИС-портала КПСиСУ
(Комитет по правовой статистике и специальным учетам ГП РК):

    https://gis.kgp.kz/arcgis/rest/services/KPSSU/crime/FeatureServer/1

Это тот же слой, что показывается на «Карте преступности» gis.kgp.kz
(Experience Builder c048e1f9... → Dashboard aae7b4c9... → webmap
d5a46535..., слой «Преступность»). В отличие от прежнего источника
(krisha.kz/ms/geodata/crime), данные официальные, полные (с 2015),
обновляются ежедневно, содержат точную дату (dat_sover), статью УК (stat),
код преступления (crime_code), тяжесть (hard_code 0-4) и адрес места.

Фильтр: только Астана (city_code='1971' — сам город; остальные коды в
bbox Астаны — соседние населённые пункты Акмолинской области) и
discontinued=0 (не снятые с учёта записи).

Режимы:
  python3 crime_collect.py            # инкремент: записи новее MAX(date)-2д
  python3 crime_collect.py --full     # полная перезаливка истории (DELETE + всё)
  python3 crime_collect.py --since 2026-07-01   # с конкретной даты

Пагинация: maxRecordCount=5000, идём resultOffset, пока не придёт пустая
страница. Политес: пауза 1.2 c между запросами (гос. сервер).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

import psycopg2
import psycopg2.extras

BASE = Path("/home/nik/krisha_bot")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")
API_URL = "https://gis.kgp.kz/arcgis/rest/services/KPSSU/crime/FeatureServer/1/query"
PAGE_SIZE = 5000          # maxRecordCount сервиса
DELAY_S = 1.2             # политес
CITY_CODE = "1971"        # Астана
OUT_FIELDS = "objectid,yr,period,crime_code,hard_code,stat,dat_sover,fz1r18p5,fz1r18p6,transgression"


def load_database_url() -> str:
    for line in (BASE / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip()
    return "postgresql://krisha@localhost/krisha_bot"


def conn():
    return psycopg2.connect(load_database_url())


def fetch_page(where: str, offset: int) -> list[dict]:
    qs = urllib.parse.urlencode({
        "where": where,
        "outFields": OUT_FIELDS,
        "orderByFields": "objectid",
        "resultOffset": str(offset),
        "resultRecordCount": str(PAGE_SIZE),
        "outSR": "4326",
        "f": "geojson",
    })
    req = urllib.request.Request(f"{API_URL}?{qs}", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=40) as r:
        data = json.loads(r.read().decode("utf-8"))
    if data.get("error"):
        raise RuntimeError(f"ArcGIS error: {data['error']}")
    return data.get("features", [])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true",
                    help="полная перезаливка истории (DELETE + качать всё)")
    ap.add_argument("--since", type=str, default=None,
                    help="качать с даты YYYY-MM-DD (инкремент)")
    a = ap.parse_args()

    db = conn()
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # определяем окно
    if a.full:
        cur.execute("DELETE FROM crime_incidents")
        db.commit()
        cutoff = date(2015, 1, 1)
        print("Режим: ПОЛНАЯ перезаливка")
    elif a.since:
        cutoff = date.fromisoformat(a.since)
        print("Режим: с даты", cutoff)
    else:
        cur.execute("SELECT COALESCE(MAX(date_excitation), '2015-01-01')::date AS md FROM crime_incidents")
        md = cur.fetchone()["md"]
        cutoff = md - timedelta(days=2)  # перекрытие на случай записей «задним числом»
        print("Режим: инкремент, cutoff =", cutoff)

    where = f"city_code='{CITY_CODE}' AND discontinued=0"
    if cutoff > date(2015, 1, 1):
        where += f" AND dat_sover >= TIMESTAMP '{cutoff.isoformat()} 00:00:00'"

    insert_sql = """
        INSERT INTO crime_incidents
            (objectid, lat, lon, crime_title, hard_code, date_excitation,
             crime_code, stat, street, house, source)
        VALUES (%(objectid)s, %(lat)s, %(lon)s, %(crime_title)s, %(hard_code)s,
                %(date_excitation)s, %(crime_code)s, %(stat)s, %(street)s,
                %(house)s, 'kgp')
        ON CONFLICT (objectid) DO NOTHING
    """

    offset = 0
    total_loaded = 0
    empty_pages = 0
    while True:
        try:
            feats = fetch_page(where, offset)
        except Exception as e:
            print("Ошибка запроса на offset", offset, ":", e)
            time.sleep(5)
            continue
        if not feats:
            empty_pages += 1
            if empty_pages >= 3:  # три пустые страницы подряд — точно конец
                break
            offset += PAGE_SIZE
            continue
        empty_pages = 0
        rows = []
        for f in feats:
            g = f.get("geometry") or {}
            coords = g.get("coordinates") or []
            if len(coords) < 2:
                continue
            p = f.get("properties") or {}
            rows.append({
                "objectid": p.get("objectid"),
                "lat": coords[1],
                "lon": coords[0],
                "crime_title": None,
                "hard_code": int(p["hard_code"]) if p.get("hard_code") not in (None, "") else None,
                "date_excitation": datetime.fromtimestamp(p["dat_sover"] / 1000).date() if p.get("dat_sover") else None,
                "crime_code": p.get("crime_code"),
                "stat": p.get("stat"),
                "street": p.get("fz1r18p5"),
                "house": p.get("fz1r18p6"),
            })
        try:
            cur.executemany(insert_sql, rows)
            db.commit()
        except Exception as e:
            db.rollback()
            print("Ошибка INSERT на offset", offset, ":", e)
            sys.exit(1)
        total_loaded += len(rows)
        print(f"offset {offset}: +{len(rows)} (всего {total_loaded})", flush=True)
        if len(feats) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
        time.sleep(DELAY_S)

    print(f"Готово. Загружено: {total_loaded}")
    cur.execute("SELECT COUNT(*) AS n FROM crime_incidents")
    print("Всего в БД:", cur.fetchone()["n"])
    db.close()


if __name__ == "__main__":
    main()
