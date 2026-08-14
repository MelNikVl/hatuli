#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Коллектор качества воздуха Астаны — официальные данные РГП «Казгидромет»
с портала открытых данных data.egov.kz (датасет atmosferalyk_aua_lastanuy_moni1,
ежемесячные сводки: город × загрязнитель → мин/макс концентрация, превышения ПДК).

Ключ API — data.egov.kz, в конфиге (bot settings AIR_API_KEY) или .env AIR_API_KEY.
Правило щадящего парсинга: пауза >= 1 c между запросами."""
import datetime
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

DATASET = "atmosferalyk_aua_lastanuy_moni1"
BASE = "https://data.egov.kz/api/v4"
CITY = "Астана"


def get_key() -> str:
    key = os.environ.get("AIR_API_KEY", "")
    if key:
        return key
    # app_settings (используется веб-приложением) или bot_settings
    for table in ("app_settings", "bot_settings"):
        try:
            r = subprocess.run(
                ["sudo", "-u", "postgres", "psql", "-d", "krisha_bot", "-t", "-A",
                 "-c", f"SELECT value FROM {table} WHERE key = 'AIR_API_KEY'"],
                capture_output=True, text=True)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except Exception:
            pass
    return ""


def api_get(version: str, size: int = 100, offset: int = 0):
    src = json.dumps({"size": size, "from": offset})
    url = f"{BASE}/{DATASET}/{version}?apiKey={urllib.parse.quote(get_key())}&source={urllib.parse.quote(src)}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/124.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def psql(sql: str) -> str:
    r = subprocess.run(["sudo", "-u", "postgres", "psql", "-d", "krisha_bot",
                        "-t", "-A", "-c", sql], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:300])
    return r.stdout.strip()


def num(v):
    """'-'/пусто/None -> None (NULL), иначе float."""
    if v is None:
        return None
    s = str(v).strip().replace(",", ".")
    if not s or s in ("-", "—"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def esc(s) -> str:
    return str(s).replace("'", "''") if s is not None else ""


def sqlv(v):
    """None -> NULL, иначе число."""
    n = num(v)
    return "NULL" if n is None else repr(n)


def main() -> int:
    key = get_key()
    if not key:
        print("❌ AIR_API_KEY не найден (env или bot_settings)")
        return 1
    # последние 4 версии (месячные сводки) — для истории
    latest_versions = [58, 57, 56, 55]  # TODO: определять список версий из API
    # более аккуратно: пробуем версии 60..50 пока не найдём с данными Астаны
    versions = []
    for v in range(60, 40, -1):
        try:
            data = api_get(f"v{v}", size=100)
            if isinstance(data, list) and any(isinstance(r, dict) and r.get("regionrus") == CITY for r in data):
                versions.append(v)
                if len(versions) >= 6:
                    break
        except Exception:
            pass
        time.sleep(1.2)
    if not versions:
        print("❌ версии с данными Астаны не найдены")
        return 1
    print("версий с данными Астаны:", versions)

    total_rows = 0
    for v in versions:
        offset = 0
        while True:
            data = api_get(f"v{v}", size=100, offset=offset)
            if not isinstance(data, list) or not data:
                break
            rows = [r for r in data if isinstance(r, dict) and r.get("regionrus") == CITY]
            for r in rows:
                poll = (r.get("dustrus") or "").strip()
                if not poll:
                    continue
                sql = ("INSERT INTO air_quality_astana "
                       "(version, pollutant, min_conc, max_conc, excess_lc, excess_mc, excess_count, excess5, excess10) "
                       "VALUES (%d, '%s', %s, %s, %s, %s, %s, %s, %s) "
                       "ON CONFLICT (version, pollutant) DO UPDATE SET "
                       "min_conc = EXCLUDED.min_conc, max_conc = EXCLUDED.max_conc, "
                       "excess_lc = EXCLUDED.excess_lc, excess_mc = EXCLUDED.excess_mc, "
                       "excess_count = EXCLUDED.excess_count, excess5 = EXCLUDED.excess5, "
                       "excess10 = EXCLUDED.excess10, fetched_at = now()"
                       % (v, esc(poll),
                          sqlv(r.get("lowconsentration")), sqlv(r.get("maxconsentration")),
                          sqlv(r.get("excesslc")), sqlv(r.get("excessmc")),
                          sqlv(r.get("excess")), sqlv(r.get("excess5")),
                          sqlv(r.get("excess10"))))
                psql(sql)
                total_rows += 1
            offset += 100
            if len(data) < 100:
                break
            time.sleep(1.2)
    print(f"upsert: {total_rows} строк (Астана, версии {versions[0]}..{versions[-1]})")

    # Сводка последней версии
    latest = versions[0]
    print("\n── Астана, версия v%d ──" % latest)
    for row in psql(
        "SELECT pollutant, max_conc, excess_count, excess_mc FROM air_quality_astana "
        "WHERE version = %d ORDER BY pollutant" % latest).splitlines():
        if row:
            print(" ", row)
    return 0


if __name__ == "__main__":
    sys.exit(main())
