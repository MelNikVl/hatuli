#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сетка качества воздуха Астаны — модель CAMS через Open-Meteo
(бесплатно, без ключа, ~11 км разрешение). Часовые значения AQI (EU) и
загрязнителей; запрос одной мульти-точкой (Open-Meteo поддерживает списки
lat/lon в одном вызове). Обновление каждые 3 часа (krisha-airgrid.timer).

Тепловая карта «Воздух» в дашборде рисует эти точки клиентской гекс-сеткой.
Правило щадящего парсинга: 1 запрос за запуск."""
import datetime
import json
import subprocess
import sys
import urllib.request

# Сетка над Астаной (чуть шире города): 6x5 = 30 точек; модель снэпает их
# в свои ячейки (~0.1°), дубликаты отбрасываем.
LATS = [51.04, 51.08, 51.12, 51.16, 51.20, 51.24]
LONS = [71.28, 71.33, 71.38, 71.43, 71.48, 71.53, 71.58]
_pts = [(la, lo) for la in LATS for lo in LONS][:30]  # 30 пар
URL = ("https://air-quality-api.open-meteo.com/v1/air-quality"
       "?latitude=" + ",".join(str(p[0]) for p in _pts) +
       "&longitude=" + ",".join(str(p[1]) for p in _pts) +
       "&hourly=european_aqi,pm2_5,pm10,nitrogen_dioxide,ozone&forecast_days=1"
       "&timezone=Asia%2FAlmaty")


def psql(sql: str) -> str:
    r = subprocess.run(["sudo", "-u", "postgres", "psql", "-d", "krisha_bot",
                        "-t", "-A", "-c", sql], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:300])
    return r.stdout.strip()


def esc(s) -> str:
    return str(s).replace("'", "''") if s is not None else ""


def main() -> int:
    req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/124.0"})
    with urllib.request.urlopen(req, timeout=90) as r:
        data = json.loads(r.read().decode("utf-8"))
    if not isinstance(data, list):
        print("❌ ответ не список:", str(data)[:200])
        return 1
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    cells: dict[tuple, dict] = {}
    for loc in data:
        h = loc.get("hourly") or {}
        lat = loc.get("latitude")
        lon = loc.get("longitude")
        if lat is None or lon is None:
            continue
        key = (round(lat, 4), round(lon, 4))
        cells[key] = {
            "aqi": h.get("european_aqi", [None])[0],
            "pm25": h.get("pm2_5", [None])[0],
            "pm10": h.get("pm10", [None])[0],
            "no2": h.get("nitrogen_dioxide", [None])[0],
            "o3": h.get("ozone", [None])[0],
        }
    print("ячеек модели в сетке:", len(cells))
    for (lat, lon), v in sorted(cells.items()):
        psql("INSERT INTO air_grid (lat, lon, aqi, pm25, pm10, no2, o3, fetched_at) "
             "VALUES (%s, %s, %s, %s, %s, %s, %s, '%s')"
             % (repr(lat), repr(lon),
                "NULL" if v["aqi"] is None else repr(v["aqi"]),
                "NULL" if v["pm25"] is None else repr(v["pm25"]),
                "NULL" if v["pm10"] is None else repr(v["pm10"]),
                "NULL" if v["no2"] is None else repr(v["no2"]),
                "NULL" if v["o3"] is None else repr(v["o3"]),
                esc(now)))
    vals = sorted((v["aqi"] for v in cells.values() if v["aqi"] is not None))
    if vals:
        print(f"AQI: мин {vals[0]}, макс {vals[-1]}, среднее {sum(vals)/len(vals):.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
