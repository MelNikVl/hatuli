"""
Rental index builder.

Парсит объявления об АРЕНДЕ с Krisha.kz:
  /arenda/kvartiry/astana/        → квартиры
  /arenda/garazhi-dachi/astana/   → паркинги/гаражи
  /arenda/kommercheskaya/astana/  → коммерческая (кладовки)

После парсинга пересчитывает rental_index:
  медиана/среднее/p25/p75 по (город, район, ЖК, комнаты, тип).

Запуск: await run_rental_cycle()
"""
from __future__ import annotations

import asyncio
import logging
import re
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE_URL = "https://krisha.kz"

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9",
}

# path → prop_type
RENTAL_PATHS: dict[str, str] = {
    "/arenda/kvartiry/astana/":       "apartment",
    "/arenda/garazhi-dachi/astana/":  "parking",
    "/arenda/kommercheskaya/astana/": "commercial",
}

MAX_PAGES_PER_PATH = 10
MIN_SLEEP = 8.0
MAX_SLEEP = 16.0


@dataclass
class RentalListing:
    id: str
    url: str
    title: str
    price: int
    area: float | None
    rooms: int | None
    floor: int | None
    floors_total: int | None
    address: str
    district: str
    complex_name: str
    city: str
    prop_type: str
    published_at: str


def _extract_int(text: str) -> int | None:
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def _extract_rooms(title: str) -> int | None:
    m = re.search(r"(\d)-комн", title.lower())
    if m:
        return int(m.group(1))
    if "студия" in title.lower():
        return 0
    return None


def _extract_area(text: str) -> float | None:
    m = re.search(r"(\d+[\.,]?\d*)\s*м²", text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "."))
    except ValueError:
        return None


def _extract_district(address: str) -> str:
    m = re.search(r"(Есиль|Алматы|Сарыарка|Нура|Байконур)", address, re.I)
    return m.group(1).capitalize() if m else ""


def _extract_complex(text: str) -> str:
    m = re.search(r"ЖК\s+[«»\"]?([А-Яа-яЁёA-Za-z0-9 \-]+)[«»\"]?", text, re.I)
    return m.group(1).strip()[:80] if m else ""


async def fetch_complex_name(listing_id: str) -> str | None:
    """Парсит страницу объявления и возвращает название ЖК."""
    url = f"{BASE_URL}/a/show/{listing_id}"
    try:
        async with httpx.AsyncClient(headers=DEFAULT_HEADERS, follow_redirects=True, timeout=15) as c:
            r = await c.get(url)
            soup = BeautifulSoup(r.text, "html.parser")
            el = soup.select_one('[data-name="map.complex"] a')
            if el:
                return el.get_text(strip=True)
    except Exception as e:
        logger.debug("fetch_complex_name %s: %s", listing_id, e)
    return None


def _parse_card(card, prop_type: str) -> RentalListing | None:
    try:
        # ID из data-id атрибута div карточки
        listing_id = card.get("data-id", "").strip()
        if not listing_id:
            return None

        # URL из ссылки внутри карточки
        link_el = card.select_one("a.a-card__title, a.a-card__image")
        url_path = link_el.get("href", "") if link_el else ""
        url = BASE_URL + url_path if url_path.startswith("/") else url_path

        # Title из ссылки заголовка или из img alt
        title_el = card.select_one("a.a-card__title")
        if title_el:
            title = title_el.get_text(strip=True)
        else:
            img = card.select_one("img.a-image__img")
            title = img.get("alt", "") if img else ""

        price_el = card.select_one(".a-card__price")
        price = _extract_int(price_el.get_text(strip=True)) if price_el else None
        if not price or price < 10_000 or price > 3_000_000:
            return None

        addr_el = card.select_one(".a-card__subtitle")
        address = addr_el.get_text(strip=True) if addr_el else ""

        district = _extract_district(address + " " + title)
        complex_name = _extract_complex(title + " " + address)

        descr_el = card.select_one(".a-card__descr")
        floor_text = descr_el.get_text(strip=True) if descr_el else ""
        floor, floors_total = None, None
        fm = re.search(r"(\d+)\s*/\s*(\d+)\s*эт", floor_text)
        if fm:
            floor, floors_total = int(fm.group(1)), int(fm.group(2))

        date_el = card.select_one(".a-card__date")
        published_at = date_el.get_text(strip=True) if date_el else ""

        return RentalListing(
            id=listing_id, url=url, title=title, price=price,
            area=_extract_area(title), rooms=_extract_rooms(title),
            floor=floor, floors_total=floors_total,
            address=address, district=district, complex_name=complex_name,
            city="astana", prop_type=prop_type, published_at=published_at,
        )
    except Exception as e:
        logger.debug("Card parse error: %s", e)
        return None


async def _fetch_page(client: httpx.AsyncClient, url: str, prop_type: str) -> list[RentalListing]:
    try:
        resp = await client.get(url, headers=DEFAULT_HEADERS, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        logger.warning("Fetch error %s: %s", url, e)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    for card in soup.select("div.a-card"):
        listing = _parse_card(card, prop_type)
        if listing:
            results.append(listing)
    return results


async def parse_rental_path(path: str, prop_type: str, max_pages: int = MAX_PAGES_PER_PATH) -> list[RentalListing]:
    all_listings: list[RentalListing] = []
    async with httpx.AsyncClient(follow_redirects=True) as client:
        for page in range(1, max_pages + 1):
            url = BASE_URL + path + (f"?page={page}" if page > 1 else "")
            listings = await _fetch_page(client, url, prop_type)
            if not listings:
                logger.info("  %s page %d: empty — stop", path, page)
                break
            all_listings.extend(listings)
            logger.info("  %s page %d: %d listings", path, page, len(listings))
            await asyncio.sleep(MIN_SLEEP + (MAX_SLEEP - MIN_SLEEP) * page / max_pages)
    return all_listings


async def save_rental_listings(listings: list[RentalListing]) -> int:
    from bot.db.pg import execute, fetchval

    saved = 0
    # Для новых объявлений — запрашиваем ЖК (не более 5 за раз, с паузой)
    new_ids = []
    for l in listings:
        exists = await fetchval("SELECT 1 FROM rental_listings WHERE id=$1", l.id)
        if not exists:
            new_ids.append(l.id)

    # Обогащаем новые объявления названием ЖК
    complex_map: dict[str, str] = {}
    for i, listing_id in enumerate(new_ids[:5]):  # не более 5 за цикл
        name = await fetch_complex_name(listing_id)
        if name:
            complex_map[listing_id] = name
            logger.info("  complex: %s → %s", listing_id, name)
        await asyncio.sleep(2)

    for l in listings:
        complex_name = complex_map.get(l.id) or l.complex_name
        try:
            await execute(
                """
                INSERT INTO rental_listings
                    (id, url, title, price, area, rooms, floor, floors_total,
                     address, district, complex_name, city, prop_type, published_at, found_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
                ON CONFLICT (id) DO UPDATE SET
                    price        = EXCLUDED.price,
                    found_at     = EXCLUDED.found_at,
                    complex_name = COALESCE(rental_listings.complex_name, EXCLUDED.complex_name)
                """,
                l.id, l.url, l.title, l.price, l.area, l.rooms, l.floor, l.floors_total,
                l.address, l.district, complex_name, l.city, l.prop_type,
                l.published_at, datetime.now(timezone.utc),
            )
            saved += 1
        except Exception as e:
            logger.warning("Save error %s: %s", l.id, e)
    return saved


async def rebuild_rental_index() -> None:
    """Пересчитывает rental_index из свежих rental_listings (последние 30 дней)."""
    from bot.db.pg import fetch, execute

    logger.info("Rebuilding rental_index...")

    rows = await fetch(
        """
        SELECT city, district, complex_name, rooms, prop_type, price, area
        FROM rental_listings
        WHERE found_at > NOW() - INTERVAL '30 days'
          AND price > 15000
          AND price < 2000000
        """
    )

    # key → prices list
    groups: dict[tuple, list[int]] = defaultdict(list)
    groups_sqm: dict[tuple, list[float]] = defaultdict(list)

    for row in rows:
        city = row["city"] or ""
        district = row["district"] or ""
        complex_name = row["complex_name"] or ""
        rooms = row["rooms"]
        prop_type = row["prop_type"] or "apartment"
        price = row["price"]
        area = row["area"]

        # Полная группа (ЖК + комнаты)
        k = (city, district, complex_name, rooms, prop_type)
        groups[k].append(price)
        if area and area > 0:
            groups_sqm[k].append(price / area)

        # Агрегат по ЖК без комнат
        k2 = (city, district, complex_name, None, prop_type)
        if k2 != k:
            groups[k2].append(price)

        # Агрегат по району
        k3 = (city, district, "", rooms, prop_type)
        groups[k3].append(price)

        k4 = (city, district, "", None, prop_type)
        if k4 != k3:
            groups[k4].append(price)

    upserted = 0
    for (city, district, complex_name, rooms, prop_type), prices in groups.items():
        if len(prices) < 2:
            continue

        ps = sorted(prices)
        n = len(ps)
        median_price = int(statistics.median(ps))
        avg_price = int(statistics.mean(ps))
        p25 = int(ps[max(0, n // 4 - 1)])
        p75 = int(ps[min(n - 1, 3 * n // 4)])

        sqm_list = groups_sqm.get((city, district, complex_name, rooms, prop_type), [])
        price_per_sqm = int(statistics.median(sqm_list)) if sqm_list else None

        cn = complex_name if complex_name else None
        d = district if district else None

        try:
            await execute(
                """
                INSERT INTO rental_index
                    (city, district, complex_name, rooms, prop_type,
                     median_price, avg_price, p25_price, p75_price,
                     sample_count, price_per_sqm, updated_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,NOW())
                ON CONFLICT (city, district, complex_name, rooms, prop_type) DO UPDATE SET
                    median_price  = EXCLUDED.median_price,
                    avg_price     = EXCLUDED.avg_price,
                    p25_price     = EXCLUDED.p25_price,
                    p75_price     = EXCLUDED.p75_price,
                    sample_count  = EXCLUDED.sample_count,
                    price_per_sqm = EXCLUDED.price_per_sqm,
                    updated_at    = NOW()
                """,
                city or None, d, cn, rooms, prop_type,
                median_price, avg_price, p25, p75, n, price_per_sqm,
            )
            upserted += 1
        except Exception as e:
            logger.warning("rental_index upsert error: %s", e)

    logger.info("rental_index rebuilt: %d groups", upserted)


async def lookup_rental_estimate(
    city: str,
    district: str | None,
    complex_name: str | None,
    rooms: int | None,
    prop_type: str = "apartment",
) -> dict | None:
    """
    Найти оценку аренды для объекта.
    Приоритет: ЖК+комнаты → ЖК → район+комнаты → район → город.
    """
    from bot.db.pg import fetchrow

    attempts = []
    if complex_name:
        attempts += [(city, district, complex_name, rooms),
                     (city, district, complex_name, None)]
    if district:
        attempts += [(city, district, None, rooms),
                     (city, district, None, None)]
    attempts.append((city, None, None, None))

    for (c, d, jk, r) in attempts:
        row = await fetchrow(
            """
            SELECT median_price, avg_price, p25_price, p75_price, sample_count, price_per_sqm
            FROM rental_index
            WHERE city = $1
              AND (district     = $2 OR ($2 IS NULL AND district IS NULL))
              AND (complex_name = $3 OR ($3 IS NULL AND complex_name IS NULL))
              AND (rooms        = $4 OR ($4 IS NULL AND rooms IS NULL))
              AND prop_type = $5
            LIMIT 1
            """,
            c, d, jk, r, prop_type,
        )
        if row:
            return dict(row)
    return None


async def run_rental_cycle() -> None:
    """Полный цикл: парсинг → сохранение → пересчёт индекса."""
    logger.info("=== Rental cycle start ===")
    total = 0
    for path, prop_type in RENTAL_PATHS.items():
        logger.info("Parsing %s (%s)...", path, prop_type)
        listings = await parse_rental_path(path, prop_type)
        saved = await save_rental_listings(listings)
        total += saved
        logger.info("  saved %d for %s", saved, prop_type)
        await asyncio.sleep(15)
    await rebuild_rental_index()
    logger.info("=== Rental cycle done: %d total ===", total)
