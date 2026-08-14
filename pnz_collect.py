#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сбор данных станций ПНЗ Астаны с портала Казгидромета ecodata.kz:3838
(app_dem_visual, Shiny). Реальные почасовые замеры 11 станций Астаны:
CO, H2S, NO, NO2, O3, PM10, PM2.5, PMtot, SO2 (мкг/м³) + прогнозы SILAM.

Механика: headless Chromium (playwright) грузит портал, ждёт Leaflet-карту,
вытаскивает маркеры станций Астаны и их попапы (таблица значений).
Обновление: каждый час (krisha-air-stations.timer).

Индекс загрязнения: max(факт / ПДК м.р.) по загрязнителям (мкг/м³).
"""
import json
import re
import subprocess
import sys
import time

URL = "http://ecodata.kz:3838/app_dem_visual/"

# ПДК макс. разовые (мкг/м³), РК — для индекса загрязнения
PDK = {
    "CO": 5000, "H2S": 8, "NO": 400, "NO2": 200, "O3": 160,
    "PM10": 300, "PM2.5": 160, "SO2": 500, "PMtot": 500,
    "фенол": 10, "формальдегид": 35, "аммиак": 200,
    "бензол": 300, "толуол": 600, "ксилол": 200, "свинец": 1,
}


def parse_popup(html: str):
    rec = {}
    m = re.search(r'<span style="color:#000000">(.*?)</span>', html, re.S)
    title = m.group(1) if m else ""
    left, sep, ts = title.partition("<tr>")
    rec["ts"] = ts.strip() or None
    if ":" in left:
        name, _, addr = left.partition(":")
        rec["name"] = name.strip()
        rec["address"] = addr.strip()
    else:
        rec["name"] = left.strip()
        rec["address"] = ""
    rows = re.findall(
        r"<td[^>]*>([^<]*)</td>\s*<td[^>]*>([^<]*)</td>\s*<td[^>]*>([^<]*)</td>\s*<td[^>]*>([^<]*)</td>",
        html)
    values = []
    for poll, fact, f24, f48 in rows:
        values.append({
            "p": poll.strip(),
            "f": _num(fact),
            "f24": _num(f24),
            "f48": _num(f48),
        })
    rec["values"] = values
    return rec


def _num(v):
    s = (v or "").strip()
    if not s or s == "—":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def compute_index(values):
    worst = None
    worst_poll = None
    for v in values:
        p = v["p"].upper()
        pdk = PDK.get(p)
        if pdk and v["f"] is not None:
            ratio = v["f"] / pdk
            if worst is None or ratio > worst:
                worst = ratio
                worst_poll = p
    return worst, worst_poll


def psql(sql: str) -> str:
    r = subprocess.run(["sudo", "-u", "postgres", "psql", "-d", "krisha_bot",
                        "-t", "-A", "-c", sql], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:300])
    return r.stdout.strip()


def main() -> int:
    from playwright.sync_api import sync_playwright
    stations = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(URL, timeout=60000, wait_until="domcontentloaded")
        for _ in range(50):
            time.sleep(1)
            n = page.evaluate("document.querySelectorAll('.leaflet-container').length")
            if n > 0:
                break
        time.sleep(5)
        data = page.evaluate(r"""() => {
          const map = window.jQuery ? jQuery('#PNZ').data('leafletMap') : null;
          if (!map) return [];
          const out = [];
          map.eachLayer(l => {
            if (l.getLatLng && l.getPopup && l.getPopup()) {
              const ll = l.getLatLng();
              const c = String(l.getPopup().getContent());
              if (/[:：]\s*(г\.\s*)?(Астана|Нур-Султан)/i.test(c)) {
                out.push({lat: ll.lat, lon: ll.lng, popup: c});
              }
            }
          });
          return out;
        }""")
        browser.close()

    for s in data:
        rec = parse_popup(s["popup"])
        rec["lat"] = round(s["lat"], 6)
        rec["lon"] = round(s["lon"], 6)
        stations.append(rec)
    if not stations:
        print("❌ станций Астаны не найдено")
        return 1

    now = time.strftime("%Y-%m-%d %H:%M:%S")
    count = 0
    for s in stations:
        idx, idx_poll = compute_index(s["values"])
        vals_json = json.dumps(s["values"], ensure_ascii=False)
        sql = ("INSERT INTO air_stations (station_name, address, lat, lon, ts, values_json, index_value, index_pollutant, fetched_at) "
               "VALUES ('%s', '%s', %s, %s, '%s', '%s', %s, '%s', '%s') "
               "ON CONFLICT (station_name, ts) DO UPDATE SET "
               "values_json = EXCLUDED.values_json, index_value = EXCLUDED.index_value, "
               "index_pollutant = EXCLUDED.index_pollutant, fetched_at = EXCLUDED.fetched_at"
               % (s.get("name", "").replace("'", "''"),
                  s.get("address", "").replace("'", "''")[:120].replace("'", "''"),
                  repr(s["lat"]), repr(s["lon"]),
                  (s.get("ts") or now).replace("'", "''"),
                  vals_json.replace("'", "''"),
                  "NULL" if idx is None else repr(round(idx, 3)),
                  (idx_poll or "").replace("'", "''"),
                  now))
        psql(sql)
        count += 1
        print(f"  {s.get('name','?')}: idx={idx:.2f} ({idx_poll})" if idx else f"  {s.get('name','?')}: нет данных")
    print(f"OK: {count} станций Астаны записано ({now})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
