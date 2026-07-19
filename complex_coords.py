"""
Заполнение координат ЖК (complexes.lat/lon) — чтобы на карте рейтинга были
ВСЕ ЖК, а не только те, у чьих объявлений уже докачаны координаты.

Три источника по приоритету:
  1. Центроид объявлений продажи (нормализованное совпадение имени) — точно
     и бесплатно, но покрывает только ЖК с координатными объявлениями.
  2. Детальная страница Korter (url в source_info) — у них карта ЖК,
     координаты лежат в HTML (ld+json / JS-конфиг).
  3. Детальная страница Homsters (через curl_cffi — анти-бот).

Запуск:
    venv/bin/python complex_coords.py --test          # показать без записи
    venv/bin/python complex_coords.py                 # центроиды + 25 страниц
    venv/bin/python complex_coords.py --pages 100     # больше страниц за раз
Повторные запуски дозаполняют только пустые. Регексы страниц написаны по
типовым паттернам — первый --test покажет реальный улов, донастроим.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import re
import sys

import httpx

sys.path.insert(0, ".")
from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cx_coords")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

# Астана: валидный диапазон
LAT_RANGE = (50.8, 51.5)
LON_RANGE = (71.0, 71.9)

_PATTERNS = [
    re.compile(r'"latitude"\s*[:=]\s*"?([\d.]+)"?[^}]{0,80}?"longitude"\s*[:=]\s*"?([\d.]+)', re.S),
    re.compile(r'"lat"\s*[:=]\s*"?([\d.]+)"?\s*,\s*"(?:lng|lon)"\s*[:=]\s*"?([\d.]+)', re.S),
    re.compile(r'data-lat="([\d.]+)"[^>]{0,120}data-(?:lng|lon)="([\d.]+)"'),
    re.compile(r'center=([\d.]+)(?:%2C|,)([\d.]+)'),
]


def _extract_coords(html: str) -> tuple[float, float] | None:
    for pat in _PATTERNS:
        for m in pat.finditer(html):
            try:
                a, b = float(m.group(1)), float(m.group(2))
            except ValueError:
                continue
            for lat, lon in ((a, b), (b, a)):
                if LAT_RANGE[0] <= lat <= LAT_RANGE[1] and LON_RANGE[0] <= lon <= LON_RANGE[1]:
                    return lat, lon
    return None


def _fetch_homsters(url: str) -> str | None:
    try:
        from curl_cffi import requests as cr
        r = cr.get(url, impersonate="chrome", timeout=30)
        return r.text if r.status_code == 200 else None
    except Exception as e:
        log.warning("homsters fetch %s: %s", url, e)
        return None


async def fill_from_centroids(execute, test: bool) -> int:
    """Центроиды объявлений -> complexes.lat/lon (нормализованный матч имени)."""
    sql = """
        UPDATE complexes c SET lat = s.lat, lon = s.lon, coords_source = 'listings'
        FROM (
            SELECT lower(trim(regexp_replace(complex_name, '^\\s*(жк|кг)\\.?\\s+', '', 'i'))) AS n,
                   AVG(lat) AS lat, AVG(lon) AS lon
            FROM apartment_listings
            WHERE lat IS NOT NULL AND complex_name IS NOT NULL AND complex_name != ''
            GROUP BY 1
        ) s
        WHERE c.lat IS NULL
          AND lower(trim(regexp_replace(c.name, '^\\s*(жк|кг)\\.?\\s+', '', 'i'))) = s.n
    """
    if test:
        log.info("[test] центроиды: пропущено (запись отключена)")
        return 0
    res = await execute(sql)
    n = int(res.split()[-1]) if res and res.split()[-1].isdigit() else 0
    log.info("центроиды объявлений: %d ЖК получили координаты", n)
    return n


async def fill_from_pages(fetch, execute, pages: int, test: bool) -> int:
    rows = await fetch("""
        SELECT id, name, source_info FROM complexes
        WHERE lat IS NULL AND source_info IS NOT NULL
        ORDER BY listings_count DESC NULLS LAST
        LIMIT $1
    """, pages)
    got = 0
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/124.0"}) as client:
        for r in rows:
            si = r["source_info"]
            if isinstance(si, str):
                try:
                    si = json.loads(si)
                except ValueError:
                    continue
            url_k = (si.get("korter") or {}).get("url")
            url_h = (si.get("homsters") or {}).get("url")
            html, source = None, None
            if url_k:
                try:
                    resp = await client.get(url_k)
                    if resp.status_code == 200:
                        html, source = resp.text, "korter"
                except Exception as e:
                    log.warning("korter %s: %s", url_k, e)
            if html is None and url_h:
                html = await asyncio.to_thread(_fetch_homsters, url_h)
                source = "homsters" if html else None
            if not html:
                continue
            coords = _extract_coords(html)
            if coords:
                got += 1
                log.info("  %s -> %.5f, %.5f (%s)", r["name"][:40], coords[0], coords[1], source)
                if not test:
                    await execute(
                        "UPDATE complexes SET lat=$2, lon=$3, coords_source=$4 WHERE id=$1",
                        r["id"], coords[0], coords[1], source)
            else:
                log.info("  %s -> координаты не найдены на странице %s", r["name"][:40], source)
            await asyncio.sleep(random.uniform(3, 6))
    log.info("со страниц источников: %d ЖК получили координаты", got)
    return got


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--pages", type=int, default=25)
    args = ap.parse_args()

    from bot.db.pg import init_pool, fetch, execute, fetchval
    await init_pool(DATABASE_URL)

    total = await fetchval("SELECT COUNT(*) FROM complexes") or 0
    have = await fetchval("SELECT COUNT(*) FROM complexes WHERE lat IS NOT NULL") or 0
    log.info("ЖК всего: %d, с координатами: %d", total, have)

    await fill_from_centroids(execute, args.test)
    await fill_from_pages(fetch, execute, args.pages, args.test)

    have2 = await fetchval("SELECT COUNT(*) FROM complexes WHERE lat IS NOT NULL") or 0
    log.info("Итог: с координатами %d из %d (%.0f%%)", have2, total,
             100.0 * have2 / total if total else 0)


if __name__ == "__main__":
    asyncio.run(main())
