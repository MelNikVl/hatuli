"""
Обогащение карточек ЖК данными со страниц ЖК на krisha.kz.

По каждому ЖК из справочника (у которого есть krisha_url — ссылка на
карточку ЖК, которую мы вытаскиваем с детальных страниц объявлений)
парсим со страницы Крыши:
  - застройщик (sidebar «Застройщик»)
  - статус строительства («Сдан в эксплуатацию» / строится)
  - срок сдачи (по очередям, как на странице)
  - официальный адрес ЖК («Расположение»)
  - рейтинг и число отзывов (JSON-LD aggregateRating)
  - фото ЖК и координаты (если у нас ещё нет)

Результат:
  complexes.developer_id / address / year_built (COALESCE — не затираем),
  lat/lon (COALESCE), photo_url (COALESCE),
  source_info->'krisha' = {developer, status, deadline, address,
                           rating, reviews_cnt, url, fetched_at}

Запуск:
    venv/bin/python krisha_complex_import.py --test          # посмотреть 5 шт, без записи
    venv/bin/python krisha_complex_import.py                 # полный прогон с записью
    venv/bin/python krisha_complex_import.py --limit 30      # первые 30
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
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup

sys.path.insert(0, ".")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("krisha_complex")

BASE = "https://krisha.kz"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9",
}

_SIDEBAR_RE = re.compile(
    r'complex__sidebar-info-title">([^<]+)</span>\s*</div>\s*'
    r'<p class="complex__sidebar-info-text">(.*?)</p>', re.S)
_RATING_RE = re.compile(
    r'"aggregateRating"\s*:\s*\{[^}]*"ratingValue"\s*:\s*"([\d,\.]+)"[^}]*'
    r'"ratingCount"\s*:\s*"(\d+)"', re.S)


def parse_complex_page(html: str, url: str) -> dict:
    """Вытаскивает все доступные поля со страницы ЖК krisha."""
    out: dict = {"url": url}

    # ── Сайдбар: застройщик / статус / срок сдачи / расположение ─────────
    sidebar = {}
    for m in _SIDEBAR_RE.finditer(html):
        label = re.sub(r"\s+", " ", m.group(1)).strip()
        value = re.sub(r"<br\s*/?>", "; ", m.group(2))
        value = re.sub(r"<[^>]+>", " ", value)
        value = re.sub(r"\s+", " ", value).strip(" ;")
        sidebar[label] = value
    out["developer"] = sidebar.get("Застройщик") or None
    out["status"] = sidebar.get("Статус строительства") or None
    deadline = sidebar.get("Срок сдачи") or ""
    out["deadline"] = deadline[:300] if deadline else None
    addr = sidebar.get("Расположение") or ""
    addr = re.sub(r"\s*Показать на карте.*$", "", addr).strip(" ,")
    out["address"] = addr or None

    # год сдачи (для сданных ЖК) — последний год из срока сдачи
    years = re.findall(r"\b(20[0-3]\d)\b", deadline)
    if years and out.get("status") and "сдан" in out["status"].lower():
        out["year_built"] = int(years[-1])

    # ── Рейтинг и число отзывов (JSON-LD) ────────────────────────────────
    rm = _RATING_RE.search(html)
    if rm:
        try:
            out["rating"] = float(rm.group(1).replace(",", "."))
            out["reviews_cnt"] = int(rm.group(2))
        except ValueError:
            pass

    # ── Фото ЖК (первое из window.data) ──────────────────────────────────
    pm = re.search(r'"photos"\s*:\s*\[\s*\{\s*"src"\s*:\s*"([^"]+)"', html)
    if pm:
        out["photo_url"] = pm.group(1)

    # ── Координаты ───────────────────────────────────────────────────────
    lat_m = re.search(r'"lat"\s*:\s*(\d{2}\.\d+)', html)
    lon_m = re.search(r'"lon"\s*:\s*(\d{2}\.\d+)', html)
    if lat_m and lon_m:
        lat, lon = float(lat_m.group(1)), float(lon_m.group(1))
        if 50.0 < lat < 53.0 and 69.0 < lon < 73.0:
            out["lat"], out["lon"] = lat, lon

    return out


async def fetch_all(limit: int = 0, delay: tuple = (4.0, 8.0)) -> dict[int, dict]:
    """{complex_id: данные} по всем ЖК с krisha_url (улицы пропускаем)."""
    from bot.db.pg import fetch, execute

    # На случай, если веб-код ещё не успел создать колонки (первый запуск скрипта)
    await execute("ALTER TABLE complexes ADD COLUMN IF NOT EXISTS is_street BOOLEAN DEFAULT FALSE")
    await execute("ALTER TABLE complexes ADD COLUMN IF NOT EXISTS krisha_url TEXT")

    rows = await fetch("""
        SELECT DISTINCT c.id, c.name,
               COALESCE(c.krisha_url,
                   (SELECT al.complex_url FROM apartment_listings al
                     WHERE lower(trim(al.complex_name)) = lower(trim(c.name))
                       AND al.complex_url IS NOT NULL LIMIT 1)) AS url
        FROM complexes c
        WHERE COALESCE(c.is_street, FALSE) = FALSE
        ORDER BY c.id
    """)
    rows = [r for r in rows if r["url"]]
    if limit:
        rows = rows[:limit]
    log.info("ЖК со ссылкой на krisha: %d", len(rows))

    found: dict[int, dict] = {}
    async with httpx.AsyncClient(headers=HEADERS, timeout=30.0,
                                 follow_redirects=True) as client:
        for i, r in enumerate(rows, 1):
            url = r["url"]
            if url.startswith("/"):
                url = BASE + url
            try:
                resp = await client.get(url)
                if resp.status_code != 200:
                    log.warning("[%d/%d] %s -> HTTP %d", i, len(rows), url, resp.status_code)
                    continue
                data = parse_complex_page(resp.text, url)
                data["name"] = r["name"]
                found[r["id"]] = data
                log.info("[%d/%d] %s: dev=%s status=%s rating=%s",
                         i, len(rows), r["name"], data.get("developer"),
                         data.get("status"), data.get("rating"))
            except Exception as e:
                log.warning("[%d/%d] %s failed: %s", i, len(rows), url, e)
            await asyncio.sleep(random.uniform(*delay))
    return found


async def save_to_db(found: dict[int, dict]) -> int:
    from bot.db.pg import fetchrow, execute

    saved = 0
    for cid, d in found.items():
        try:
            row = await fetchrow("SELECT source_info FROM complexes WHERE id=$1", cid)
            if not row:
                continue
            si = row["source_info"]
            if isinstance(si, str):
                try:
                    si = json.loads(si)
                except ValueError:
                    si = {}
            si = si or {}
            si["krisha"] = {
                "developer": d.get("developer"),
                "status": d.get("status"),
                "deadline": d.get("deadline"),
                "address": d.get("address"),
                "rating": d.get("rating"),
                "reviews_cnt": d.get("reviews_cnt"),
                "url": d.get("url"),
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }

            developer_id = None
            if d.get("developer"):
                developer_id = await fetchrow(
                    "INSERT INTO developers (name) VALUES ($1) "
                    "ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name "
                    "RETURNING id", d["developer"].strip())
                developer_id = developer_id["id"] if developer_id else None

            # Координаты с официальной карточки Крыши — приоритетнее
            # вычисленных: перезаписываем, если расходятся > ~250 м
            await execute("""
                UPDATE complexes SET
                    developer_id = COALESCE(developer_id, $2),
                    address      = COALESCE(address, $3),
                    year_built   = COALESCE(year_built, $4),
                    lat          = CASE WHEN $5 IS NOT NULL AND (
                                        lat IS NULL OR
                                        (lat - $5)^2 + (lon - $6)^2 > 8.0e-6)
                                   THEN $5 ELSE lat END,
                    lon          = CASE WHEN $5 IS NOT NULL AND (
                                        lat IS NULL OR
                                        (lat - $5)^2 + (lon - $6)^2 > 8.0e-6)
                                   THEN $6 ELSE lon END,
                    photo_url    = COALESCE(photo_url, $7),
                    krisha_url   = COALESCE(krisha_url, $8),
                    source_info  = $9::jsonb,
                    updated_at   = now()
                WHERE id = $1
            """, cid, developer_id, d.get("address"), d.get("year_built"),
                d.get("lat"), d.get("lon"), d.get("photo_url"), d.get("url"),
                json.dumps(si, ensure_ascii=False, default=str))
            saved += 1
        except Exception as e:
            log.warning("save complex %s failed: %s", cid, e)
    return saved


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="только показать 5, без записи")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    from dotenv import load_dotenv
    load_dotenv()
    from bot.db.pg import init_pool
    await init_pool(os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot"))

    found = await fetch_all(limit=args.limit or (5 if args.test else 0))
    if args.test:
        for cid, d in found.items():
            print(json.dumps({"id": cid, **d}, ensure_ascii=False, indent=2, default=str))
        return
    saved = await save_to_db(found)
    log.info("=== krisha complex import done: %d/%d сохранено ===", saved, len(found))


if __name__ == "__main__":
    asyncio.run(main())
