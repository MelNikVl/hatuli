"""
Rental index builder.

Парсит объявления об АРЕНДЕ с Krisha.kz:
  /arenda/kvartiry/astana/        → квартиры
  /arenda/garazhi/astana/        → паркинги/гаражи
  /arenda/kommercheskaya-nedvizhimost/astana/ → коммерческая

После парсинга пересчитывает rental_index:
  медиана/среднее/p25/p75 по (город, район, ЖК, комнаты, тип).

Запуск: await run_rental_cycle()
"""
from __future__ import annotations

import asyncio
import logging
import hashlib
import json
import re
import statistics
import time
import random
from urllib.parse import parse_qs, urlparse
from collections import defaultdict
from dataclasses import dataclass, field
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
    "/arenda/garazhi/astana/":       "parking",
    "/arenda/kommercheskaya-nedvizhimost/astana/": "commercial",
}

MAX_PAGES_PER_PATH = 0  # 0/None means scan to the end, not a page cap
RENTAL_INDEX_REFRESH_PAGES = 15
MIN_SLEEP = 8.0
MAX_SLEEP = 16.0


@dataclass
class RentalListing:
    id: str
    url: str
    title: str
    price: int | None
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
    complex_url: str | None = None
    photo_url: str | None = None


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


def _rental_card_photo_url(value: object) -> str | None:
    """Allow observed rental preview CDNs without changing fingerprint rules."""
    from bot.core.daily_picks import safe_photo_url
    approved = safe_photo_url(value)
    if approved:
        return approved
    if not isinstance(value, str) or len(value) > 400:
        return None
    try:
        parsed = urlparse(value)
        if (parsed.scheme != "https" or parsed.hostname != "astps-photos-kr.kcdn.kz"
                or parsed.username or parsed.password or parsed.port is not None
                or parsed.query or parsed.fragment or parsed.params):
            return None
        match = re.fullmatch(
            r"/webp/[0-9a-f]{2}/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/"
            r"\d{1,4}-(\d{1,4})x(\d{1,4})\.(?:jpg|jpeg|webp)", parsed.path, re.I)
        if not match or any(not 1 <= int(size) <= 4096 for size in match.groups()):
            return None
    except ValueError:
        return None
    return value


async def fetch_complex_name(listing_id: str) -> str | None:
    """Парсит страницу объявления и возвращает название ЖК."""
    url = f"{BASE_URL}/a/show/{listing_id}"
    try:
        async with httpx.AsyncClient(headers=DEFAULT_HEADERS, follow_redirects=True, timeout=15) as c:
            r = await c.get(url)
            soup = BeautifulSoup(r.text, "html.parser")
            el = soup.select_one('[data-name="map.complex"] a')
            if el:
                name = el.get_text(strip=True)
                # Фильтр мусора: улицы, адреса, числа
                if name and len(name) > 2 and not any(x in name.lower() for x in [
                    "улица", "проспект", "бульвар", "переулок",
                    "на ", "ул.", "пр.", "д.", "кв.",
                ]) and not name[0].isdigit():
                    return name
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
        price_text = price_el.get_text(" ", strip=True) if price_el else ""
        price_match = re.search(r"\d[\d\s\u00a0]*", price_text)
        price = _extract_int(price_match.group(0)) if price_match else None
        # The collection inventory includes negotiated/missing prices too.
        # Commercial offers can legitimately exceed the former apartment cap.
        if price is not None and not 0 < price <= 2_147_483_647:
            price = None

        addr_el = card.select_one(".a-card__subtitle")
        address = addr_el.get_text(strip=True) if addr_el else ""

        district = _extract_district(address + " " + title)
        complex_name = _extract_complex(title + " " + address)
        complex_url = None
        for complex_link in card.select('a[href*="/complex/show/"]'):
            href = complex_link.get("href", "")
            parsed = urlparse(href)
            if (parsed.netloc in ("", "krisha.kz") and parsed.scheme in ("", "http", "https") and
                    re.fullmatch(r"/complex/show/(?:\d+|astana/[A-Za-z0-9_%\-]+)/?", parsed.path)):
                name = complex_link.get_text(" ", strip=True)
                if name and len(name) > 2:
                    complex_name = re.sub(r"^ЖК\s+", "", name, flags=re.I).strip()
                    complex_url = BASE_URL + parsed.path
                    break

        descr_el = card.select_one(".a-card__descr")
        floor_text = descr_el.get_text(strip=True) if descr_el else ""
        floor, floors_total = None, None
        fm = re.search(r"(\d+)\s*/\s*(\d+)\s*эт", floor_text)
        if fm:
            floor, floors_total = int(fm.group(1)), int(fm.group(2))

        date_el = card.select_one(".a-card__date")
        published_at = date_el.get_text(strip=True) if date_el else ""
        photo_url = None
        photo = card.select_one("img.a-image__img")
        if photo:
            for attribute in ("src", "data-src", "data-original"):
                value = photo.get(attribute)
                if isinstance(value, str):
                    value = "https:" + value if value.startswith("//") else value
                candidate = _rental_card_photo_url(value)
                if candidate:
                    photo_url = candidate
                    break

        return RentalListing(
            id=listing_id, url=url, title=title, price=price,
            area=_extract_area(title), rooms=_extract_rooms(title),
            floor=floor, floors_total=floors_total,
            address=address, district=district, complex_name=complex_name,
            city="astana", prop_type=prop_type, published_at=published_at,
            complex_url=complex_url,
            photo_url=photo_url,
        )
    except Exception as e:
        logger.debug("Card parse error: %s", e)
        return None


@dataclass
class RentalPage:
    listings: list[RentalListing]
    card_ids: tuple[str, ...]
    last_page: int | None = None
    has_next: bool | None = None
    # A last-page link/ellipsis proves the catalog boundary; a window of
    # neighbouring page numbers alone does not prove the last page.
    last_page_confirmed: bool = False
    total_count: int | None = None


@dataclass
class RentalPagination:
    """Durable list-page progress, including evidence of a complete sweep."""
    page: int = 1
    signatures: set[str] = field(default_factory=set)
    seen_ids: set[str] = field(default_factory=set)
    pages_seen: int = 0
    started_from_first: bool = False
    expected_last_page: int | None = None
    expected_total: int | None = None

    def advance(self, result: RentalPage) -> bool:
        signature = hashlib.sha256("\n".join(sorted(set(result.card_ids))).encode()).hexdigest()
        repeated = signature in self.signatures
        # End is established by a successfully parsed catalog response. A
        # block, empty skeleton or arbitrary repeated middle page is not EOF.
        terminal = (not result.card_ids or result.has_next is False or
                    (repeated and result.last_page is not None and
                     self.page >= result.last_page))
        if repeated and result.last_page is None and not terminal:
            raise ValueError("Repeated rental page without confirmed catalog end")
        self.seen_ids.update(result.card_ids)
        self.pages_seen += 1
        if result.last_page_confirmed and result.last_page:
            self.expected_last_page = result.last_page
        if result.total_count is not None:
            self.expected_total = result.total_count
        if terminal:
            self.page = 1
            self.signatures.clear()
            return True
        self.signatures.add(signature)
        self.page += 1
        return False

    def dumps(self) -> str:
        return json.dumps({"page": self.page, "signatures": sorted(self.signatures),
                           "seen_ids": sorted(self.seen_ids), "pages_seen": self.pages_seen,
                           "started_from_first": self.started_from_first,
                           "expected_last_page": self.expected_last_page,
                           "expected_total": self.expected_total})

    @classmethod
    def loads(cls, value: str | None) -> "RentalPagination":
        if not value:
            return cls()
        state = json.loads(value)
        if not isinstance(state, dict):
            raise ValueError("Invalid rental cursor")
        page, signatures = state.get("page"), state.get("signatures")
        if type(page) is not int or page < 1 or not isinstance(signatures, list):
            raise ValueError("Invalid rental cursor")
        if any(not isinstance(v, str) or not re.fullmatch(r"[a-f0-9]{64}", v) for v in signatures):
            raise ValueError("Invalid rental page signatures")
        seen_ids = state.get("seen_ids", [])
        if not isinstance(seen_ids, list) or any(not isinstance(v, str) or not v.isdigit() for v in seen_ids):
            raise ValueError("Invalid rental observed IDs")
        pages_seen = state.get("pages_seen", 0)
        started = state.get("started_from_first", False)
        if type(pages_seen) is not int or pages_seen < 0 or type(started) is not bool:
            raise ValueError("Invalid rental progress evidence")
        optional = [state.get("expected_last_page"), state.get("expected_total")]
        if any(v is not None and (type(v) is not int or v < 0) for v in optional):
            raise ValueError("Invalid rental expected catalog size")
        return cls(page, set(signatures), set(seen_ids), pages_seen, started, *optional)


class RentalSearchBlockedError(ValueError):
    """HTTP-200 challenge: retain a distinguishable block without treating it as EOF."""
    source_blocked = True
    status_code = 200


def _catalog_pagination(soup: BeautifulSoup, requested_page: int) -> tuple[int | None, bool | None, bool]:
    containers = soup.select('.paginator, .pagination, [data-pagination]')
    links = [link for container in containers for link in container.select('a[href]')]
    # Some templates put rel=last outside the visible paginator.
    links += soup.select('a[rel="last"][href]')
    numbers = []
    last_confirmed = False
    for link in links:
        match = re.search(r"[?&]page=(\d+)", link.get("href", ""))
        if not match:
            continue
        number = int(match.group(1))
        numbers.append(number)
        if "last" in (link.get("rel") or []):
            last_confirmed = True
    maximum = max(numbers) if numbers else None
    if maximum is not None:
        for container in containers:
            text = container.get_text(" ", strip=True)
            if re.search(r"\.{3}|…", text) and any(
                    re.search(rf"[?&]page={maximum}(?:&|$)", link.get("href", ""))
                    and link.get_text(strip=True) == str(maximum)
                    for link in container.select('a[href]')):
                last_confirmed = True
        # A fully visible small paginator ending at page 2/3 can be treated
        # as terminal only while fetching that page, never as a head-size proof.
        has_next = any(number > requested_page for number in numbers)
    else:
        has_next = None
    return maximum, has_next, last_confirmed


async def _fetch_page_result(client: httpx.AsyncClient, url: str, prop_type: str) -> RentalPage:
    resp = await client.get(url, headers=DEFAULT_HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    text = soup.get_text(" ", strip=True).lower()
    explicit_challenge = re.search(
        r"подтвердите.{0,30}(робот|человек)|access denied|just a moment", text)
    challenge = re.search(r"captcha|капч|подтвердите.{0,30}(робот|человек)|провер.{0,20}(робот|человек)|access denied|just a moment", text)
    main_list = soup.select_one(".a-list.a-search-list")
    cards = (main_list.select(":scope > div.a-card, :scope > section.a-card") if main_list is not None
             else soup.select("div.a-card, section.a-card"))
    if explicit_challenge or (challenge and not cards):
        raise RentalSearchBlockedError(f"Blocked rental search response: {url}")
    if not cards:
        explicit_empty = soup.select_one(".a-list-empty") is not None or re.search(
            r"ничего не найдено|объявлени[йя] не найдено|найдено\s+0\s+объявлен", text)
        if not explicit_empty:
            raise ValueError(f"Unrecognised rental search response: {url}")
        return RentalPage([], (), has_next=False, total_count=0)

    requested_page = int(parse_qs(urlparse(url).query).get("page", ["1"])[0])
    last_page, has_next, last_confirmed = _catalog_pagination(soup, requested_page)
    count_match = re.search(r"найдено\s+([\d\s\u00a0]+)\s+объявлен", text)
    total_count = _extract_int(count_match.group(1)) if count_match else None
    results, card_ids = {}, []
    for card in cards:
        listing_id = str(card.get("data-id", "")).strip()
        if not listing_id:
            link = card.select_one('a[href*="/a/show/"]')
            match = re.search(r"/a/show/(\d+)", link.get("href", "")) if link else None
            listing_id = match.group(1) if match else ""
        if not listing_id or not listing_id.isdigit():
            raise ValueError(f"Rental card without a valid ID: {url}")
        card_ids.append(listing_id)
        # The template occasionally has only the detail URL. Use its ID in
        # both coverage accounting and the persisted card, not just the former.
        if not card.get("data-id"):
            card["data-id"] = listing_id
        listing = _parse_card(card, prop_type)
        if listing:
            results[listing.id] = listing
    return RentalPage(list(results.values()), tuple(card_ids), last_page,
                      has_next, last_confirmed, total_count)


async def _fetch_page(client: httpx.AsyncClient, url: str, prop_type: str) -> list[RentalListing]:
    """Compatibility wrapper. Fetch failures propagate; they are not EOF."""
    return (await _fetch_page_result(client, url, prop_type)).listings


async def collect_rental_page(client: httpx.AsyncClient, path: str, prop_type: str,
                              *, refresh_head: bool = False) -> dict:
    """One durable page, optionally preceded by a bounded fresh-list check.

    A hot page never advances deep coverage. It is reused when the cursor is
    at page one. A validated smaller catalog resets an out-of-range old cursor
    as an aborted round, never a completed one. No detail request is made.
    """
    from bot.db import settings as app_settings
    await app_settings.load()
    key = f"RENTAL_CRAWL_V1_{prop_type.upper()}"
    cursor = RentalPagination.loads(app_settings.get(key, ""))
    metrics = {"requests": 0, "head_pages": 0, "durable_pages": 0,
               "saved": 0, "new": 0, "updated": 0, "reactivated": 0,
               "raw_cards": 0, "accepted_cards": 0, "unpriced_cards": 0}
    reconciled = False

    async def fetch_page(page: int) -> RentalPage:
        metrics["requests"] += 1
        url = BASE_URL + path + (f"?page={page}" if page > 1 else "")
        return await _fetch_page_result(client, url, prop_type)

    async def save_page(result: RentalPage) -> None:
        metrics["raw_cards"] += len(result.card_ids)
        metrics["accepted_cards"] += len(result.listings)
        metrics["unpriced_cards"] += sum(listing.price is None for listing in result.listings)
        count = await save_rental_listings(result.listings) if result.listings else 0
        metrics["saved"] += int(count)
        for name in ("new", "updated", "reactivated"):
            metrics[name] += getattr(count, name, 0)
        if count != len(result.listings):
            raise RuntimeError(f"Incomplete rental page save: {count}/{len(result.listings)}")

    try:
        head = await fetch_page(1) if refresh_head else None
        if head is not None:
            await save_page(head)
            metrics["head_pages"] = 1
            if head.last_page_confirmed and head.last_page and cursor.page > head.last_page:
                reconciled = True
                await app_settings.set(f"{key}_ABORTED_AT", datetime.now(timezone.utc).isoformat())
                await app_settings.set(f"{key}_ABORT_REASON", "cursor_beyond_confirmed_catalog_end")
                cursor = RentalPagination()
                await app_settings.set(key, cursor.dumps())
            if head.last_page_confirmed:
                await app_settings.set(f"{key}_EXPECTED_LAST_PAGE", str(head.last_page or 0))
            if head.total_count is not None:
                await app_settings.set(f"{key}_EXPECTED_TOTAL", str(head.total_count))

        page = cursor.page
        if page == 1 and head is not None:
            result = head
        else:
            if head is not None:
                await asyncio.sleep(random.uniform(MIN_SLEEP, MAX_SLEEP))
            result = await fetch_page(page)
            await save_page(result)
        now = datetime.now(timezone.utc)
        if page == 1 and cursor.pages_seen == 0:
            cursor.started_from_first = True
            await app_settings.set(f"{key}_STARTED_AT", now.isoformat())
        if head is not None and head.last_page_confirmed:
            cursor.expected_last_page = head.last_page
        if head is not None and head.total_count is not None:
            cursor.expected_total = head.total_count
        ended = cursor.advance(result)
        # Old cursors only contain signatures/page. Finishing their tail does
        # not prove pages 1..N were scanned successfully in this deployment.
        completed = ended and cursor.started_from_first and cursor.pages_seen >= page
        metrics["durable_pages"] = 1
        if ended:
            started = app_settings.get(f"{key}_STARTED_AT", "")
            duration = 0
            try:
                start_dt = datetime.fromisoformat(started) if started else None
                if start_dt is not None:
                    start_dt = start_dt if start_dt.tzinfo else start_dt.replace(tzinfo=timezone.utc)
                    duration = max(0, int((now - start_dt).total_seconds()))
            except ValueError:
                pass
            prefix = key if completed else f"{key}_INCOMPLETE"
            await app_settings.set(f"{prefix}_COMPLETED_AT", now.isoformat())
            await app_settings.set(f"{prefix}_DURATION_SEC", str(duration))
            await app_settings.set(f"{prefix}_LAST_PAGES", str(cursor.pages_seen))
            await app_settings.set(f"{prefix}_UNIQUE_SEEN", str(len(cursor.seen_ids)))
            await app_settings.set(f"{prefix}_TOTAL", str(cursor.expected_total or 0))
            if completed:
                await app_settings.set(f"{key}_COMPLETED_VERIFIED", "1")
            await app_settings.set(f"{key}_LAST_STATUS", "complete" if completed else "incomplete_legacy_tail")
            await app_settings.set(key, RentalPagination().dumps())
        else:
            await app_settings.set(key, cursor.dumps())
            await app_settings.set(f"{key}_LAST_STATUS", "collecting")
        await app_settings.set(f"{key}_LAST_SUCCESS_AT", now.isoformat())
        return {"page": page, **metrics, "completed": completed, "round_ended": ended,
                "cursor_reconciled": reconciled, "raw_card_count": len(result.card_ids),
                "accepted_card_count": len(result.listings),
                "unpriced_card_count": sum(listing.price is None for listing in result.listings),
                "unique_seen": len(cursor.seen_ids), "expected_last_page": cursor.expected_last_page,
                "expected_total": cursor.expected_total}
    except Exception as exc:
        # A successful head save remains measurable if the deep request fails.
        exc.rental_metrics = metrics
        raise


async def parse_rental_path(path: str, prop_type: str, max_pages: int | None = MAX_PAGES_PER_PATH) -> list[RentalListing]:
    """Unbounded by default; an explicit positive limit is for partial scans."""
    all_listings: dict[str, RentalListing] = {}
    cursor = RentalPagination()
    async with httpx.AsyncClient(follow_redirects=True) as client:
        while not max_pages or cursor.page <= max_pages:
            page = cursor.page
            url = BASE_URL + path + (f"?page={page}" if page > 1 else "")
            result = await _fetch_page_result(client, url, prop_type)
            all_listings.update((listing.id, listing) for listing in result.listings)
            if cursor.advance(result):
                logger.info("  %s page %d: end or repeated page", path, page)
                break
            logger.info("  %s page %d: %d listings", path, page, len(result.listings))
            await asyncio.sleep(MIN_SLEEP + (MAX_SLEEP - MIN_SLEEP) * min(page / (max_pages or page), 1))
    return list(all_listings.values())


class RentalSaveCount(int):
    """Keep the legacy integer API while exposing successful write outcomes."""

    def __new__(cls, saved: int, *, new: int = 0, updated: int = 0,
                reactivated: int = 0):
        value = super().__new__(cls, saved)
        value.new = new
        value.updated = updated
        value.reactivated = reactivated
        return value


async def save_rental_listings(listings: list[RentalListing]) -> int:
    """Save list observations without issuing detail HTTP requests.

    Official complex/coordinates are filled by the separately budgeted detail
    worker. Price history and the successful observation share one SQL
    statement, so a failed upsert cannot leave a phantom price change.
    Schema setup belongs to migrations, never the per-page hot path.
    """
    from bot.db.pg import fetchrow

    saved = new = updated = reactivated = 0
    # Promoted cards may repeat the same ID within a page. Count observations
    # once, preserving the final card just as the full-path parser does.
    unique = {listing.id: listing for listing in listings}
    for listing in unique.values():
        try:
            outcome = await fetchrow(
                """
                WITH previous AS MATERIALIZED (
                    SELECT id, price, is_active FROM rental_listings
                     WHERE id = $1 FOR UPDATE
                ), observed AS (
                    INSERT INTO rental_listings
                        (id, url, title, price, area, rooms, floor, floors_total,
                         address, district, complex_name, city, prop_type,
                         published_at, found_at, last_seen, complex_url, photo_url)
                    SELECT $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$15,$16,$17
                      FROM (SELECT count(*) FROM previous) previous_observation
                     WHERE TRUE
                    ON CONFLICT (id) DO UPDATE SET
                        price = EXCLUDED.price,
                        last_seen = EXCLUDED.last_seen,
                        complex_name = CASE WHEN EXCLUDED.complex_url IS NOT NULL
                            THEN NULLIF(btrim(EXCLUDED.complex_name), '')
                            ELSE COALESCE(NULLIF(btrim(rental_listings.complex_name), ''),
                                          NULLIF(btrim(EXCLUDED.complex_name), '')) END,
                        complex_url = COALESCE(EXCLUDED.complex_url, rental_listings.complex_url),
                        photo_url = COALESCE(EXCLUDED.photo_url, rental_listings.photo_url),
                        url = EXCLUDED.url,
                        title = EXCLUDED.title,
                        area = EXCLUDED.area,
                        rooms = EXCLUDED.rooms,
                        floor = EXCLUDED.floor,
                        floors_total = EXCLUDED.floors_total,
                        address = EXCLUDED.address,
                        district = EXCLUDED.district,
                        city = EXCLUDED.city,
                        prop_type = EXCLUDED.prop_type,
                        published_at = EXCLUDED.published_at,
                        is_active = TRUE,
                        archived_at = NULL,
                        archive_reason = NULL
                    RETURNING id, price, (xmax = 0) AS is_new
                ), changed AS (
                    INSERT INTO rental_price_history (listing_id, old_price, new_price)
                    SELECT observed.id, previous.price, observed.price
                      FROM observed JOIN previous USING (id)
                     WHERE previous.price > 0 AND observed.price > 0
                       AND previous.price <> observed.price
                    RETURNING listing_id
                )
                SELECT observed.is_new,
                       EXISTS (SELECT 1 FROM previous WHERE is_active IS FALSE) AS reactivated
                  FROM observed
                """,
                listing.id, listing.url, listing.title, listing.price,
                listing.area, listing.rooms, listing.floor, listing.floors_total,
                listing.address, listing.district, listing.complex_name,
                listing.city, listing.prop_type, listing.published_at,
                datetime.now(timezone.utc), listing.complex_url, listing.photo_url,
            )
            if outcome is None:
                raise RuntimeError("Rental upsert returned no outcome")
            saved += 1
            new += int(outcome["is_new"])
            updated += int(not outcome["is_new"])
            reactivated += int(outcome["reactivated"])
        except Exception as exc:
            logger.warning("Save error %s: %s", listing.id, exc)
    return RentalSaveCount(saved, new=new, updated=updated, reactivated=reactivated)


async def rebuild_rental_index() -> None:
    """Atomically replace rental aggregates with fresh, active monthly offers."""
    from bot.db.pg import fetch, get_pool
    from bot.core.comparison_rental_estimate import _NONMONTHLY, RENTAL_COMPARISON_SQL

    logger.info("Rebuilding rental_index...")

    rows = await fetch(
        """
        SELECT id, city, district, complex_name, complex_url, rooms, prop_type, price, area, title
        FROM rental_listings
        WHERE last_seen BETWEEN NOW() - INTERVAL '72 hours' AND NOW()
          AND city = 'astana'
          AND prop_type = 'apartment'
          AND is_active IS TRUE
          AND archived_at IS NULL
          AND COALESCE(is_duplicate, FALSE) = FALSE
          AND COALESCE(dup_needs_review, FALSE) = FALSE
          AND NULLIF(btrim(duplicate_of), '') IS NULL
          AND price > 0
        """
    )
    # The legacy URL may have come from a related-project link. The same
    # source-identity rule as public rent comparisons must approve its match
    # to an unambiguous canonical JK; a nonempty URL alone proves nothing.
    verified_ids = {
        str(row["id"]) for row in await fetch(RENTAL_COMPARISON_SQL, datetime.now(timezone.utc))
        if row.get("complex_binding_verified") is True
    }

    # key → prices list
    groups: dict[tuple, list[int]] = defaultdict(list)
    groups_sqm: dict[tuple, list[float]] = defaultdict(list)

    for row in rows:
        if _NONMONTHLY.search(row["title"] or ""):
            continue
        city = row["city"] or ""
        district = row["district"] or ""
        complex_name = (row["complex_name"] or "") if str(row["id"]) in verified_ids else ""
        rooms = row["rooms"]
        prop_type = row["prop_type"] or "apartment"
        price = row["price"]
        area = row["area"]
        sqm = price / area if area and area > 0 else None

        # Set semantics matter when district/complex are absent: city keys
        # coincide with narrower keys and previously counted a row 2–6 times.
        keys = {(city, district, complex_name, rooms, prop_type),
                (city, district, complex_name, None, prop_type),
                (city, district, "", rooms, prop_type),
                (city, district, "", None, prop_type),
                (city, "", "", rooms, prop_type),
                (city, "", "", None, prop_type)}
        for key in keys:
            groups[key].append(price)
            if sqm is not None:
                groups_sqm[key].append(sqm)

    records = []
    for (city, district, complex_name, rooms, prop_type), prices in groups.items():
        if len(prices) < 5:
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

        records.append((city or None, d, cn, rooms, prop_type,
                        median_price, avg_price, p25, p75, n, price_per_sqm))

    async with get_pool().acquire() as connection:
        async with connection.transaction():
            await connection.execute("DELETE FROM rental_index")
            if records:
                await connection.executemany(
                    """
                    INSERT INTO rental_index
                        (city, district, complex_name, rooms, prop_type,
                         median_price, avg_price, p25_price, p75_price,
                         sample_count, price_per_sqm, updated_at)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,NOW())
                    """, records)
    logger.info("rental_index rebuilt: %d groups", len(records))


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
    # The apartment category establishes monthly scope. Parking/commercial
    # list cards can mix hourly and per-m² offers; the schema has no unit yet.
    if prop_type != "apartment":
        return None
    district = _extract_district(district or "") or district

    # ВАЖНО: комнатность сохраняем как можно дольше — аренда однушки,
    # применённая к трёшке, даёт мусорный yield. Порядок:
    # ЖК+комнаты → район+комнаты → город+комнаты → район → город.
    attempts = []
    if complex_name:
        attempts.append((city, district, complex_name, rooms, "ЖК"))
    if district:
        attempts.append((city, district, None, rooms, "район"))
    attempts.append((city, None, None, rooms, "город"))
    if district:
        attempts.append((city, district, None, None, "район (все комн.)"))
    attempts.append((city, None, None, None, "город (все комн.)"))

    for (c, d, jk, r, level) in attempts:
        row = await fetchrow(
            """
            SELECT median_price, avg_price, p25_price, p75_price, sample_count, price_per_sqm
            FROM rental_index
            WHERE city = $1
              AND (district     = $2 OR ($2 IS NULL AND district IS NULL))
              AND (complex_name = $3 OR ($3 IS NULL AND complex_name IS NULL))
              AND (rooms        = $4 OR ($4 IS NULL AND rooms IS NULL))
              AND prop_type = $5
              AND updated_at BETWEEN NOW() - INTERVAL '72 hours' AND NOW()
              AND sample_count >= 5
            LIMIT 1
            """,
            c, d, jk, r, prop_type,
        )
        if row:
            result = dict(row)
            result["level"] = level
            return result
    return None


async def run_rental_cycle() -> None:
    """Полный цикл: парсинг → сохранение → пересчёт индекса."""
    logger.info("=== Rental cycle start (all pages) ===")
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


# ── Бэкфилл привязки аренды: ЖК (офиц. блок Крыши) + координаты + адрес ──

async def backfill_rental_details(batch: int = 8, *, budget_seconds: float = 30,
                                  progress: dict | None = None) -> dict:
    """Докачивает детальные страницы аренды: у каждого объявления должна
    быть привязка — ЖК (официальный блок map.complex), координаты
    (JS-блоб страницы) или хотя бы адрес. Берёт только живые объявления."""
    import random as _rnd
    from bot.db.pg import fetch, execute

    from bot.core.apartment_details import (fetch_apartment_details, ListingBlockedError,
                                           detail_fetch_cooldown_remaining, REQUEST_COUNTS)
    report = {} if progress is None else progress
    report.update(checked=0, coords=0, complex=0, requests=0, http_468=0,
                  detail_attempts=0, blocked=0, errors=0, budget_exhausted=False)
    if batch <= 0 or budget_seconds <= 0:
        return report
    if detail_fetch_cooldown_remaining():
        report["cooldown"] = True
        return report
    deadline = time.monotonic() + budget_seconds

    rows = await fetch("""
        SELECT id, url FROM rental_listings
        WHERE (lat IS NULL OR complex_name IS NULL OR btrim(complex_name) = ''
               OR complex_url IS NULL OR btrim(complex_url) = '')
          AND is_active IS NOT FALSE
          AND COALESCE(is_duplicate, FALSE) = FALSE
          AND COALESCE(dup_needs_review, FALSE) = FALSE
          AND url IS NOT NULL
          AND last_seen > now() - interval '7 days'
          AND (coord_fetch_attempted_at IS NULL
               OR coord_fetch_attempted_at < now() - interval '3 days')
        ORDER BY coord_fetch_attempted_at ASC NULLS FIRST, found_at DESC
        LIMIT $1
    """, batch)
    if not rows:
        return report

    # Detail fetches use their own cooldown; list collection never consults it.
    from bot.core.complex_audit import street_names
    streets = await street_names()

    got_geo = got_cx = 0
    for r in rows:
        if detail_fetch_cooldown_remaining():
            report["cooldown"] = True
            break
        pause = _rnd.uniform(8.0, 15.0)
        remaining = deadline - time.monotonic()
        # Leave enough time for the shared fetcher's own 3–6 s pacing.
        if remaining <= pause + 6:
            report["budget_exhausted"] = True
            break
        await asyncio.sleep(pause)
        if detail_fetch_cooldown_remaining():
            report["cooldown"] = True
            break
        before_requests = REQUEST_COUNTS["detail"]
        report["detail_attempts"] += 1
        try:
            details = await asyncio.wait_for(
                fetch_apartment_details(r["url"], raise_on_error=True),
                timeout=max(0.1, deadline - time.monotonic()))
        except ListingBlockedError as exc:
            report["blocked"] += 1
            report["http_468"] += int(str(exc).startswith("468 "))
            break  # do not mark this or remaining rows as failed fetches
        except asyncio.TimeoutError:
            report["errors"] += 1
            report["budget_exhausted"] = True
            break
        except Exception as e:
            logger.warning("rental backfill fetch failed %s: %s", r["id"], e)
            report["errors"] += 1
            details = None
        finally:
            report["requests"] += max(0, REQUEST_COUNTS["detail"] - before_requests)
        report["checked"] += 1
        if not details:
            await execute(
                "UPDATE rental_listings SET coord_fetch_attempted_at=now() WHERE id=$1",
                r["id"])
            continue

        cx = (details.get("complex_name")
              if details.get("complex_source") == "official_map_block" else None)
        if cx:
            n = re.sub(r"^\s*(жк|кг)\.?\s+", "", cx.lower())
            n = re.sub(r"[«»\"'()]", " ", n)
            n = re.sub(r"\s+", " ", n).strip()
            from bot.core.complex_audit import _JUNK_NAMES
            if n in streets or n in _JUNK_NAMES or len(n) < 4:
                cx = None  # ЖК-улица / мусорное имя — не привязываем

        sets, params, i = ["coord_fetch_attempted_at=now()"], [], 1
        if details.get("lat") and details.get("lon"):
            sets.append(f"lat=${i}"); params.append(details["lat"]); i += 1
            sets.append(f"lon=${i}"); params.append(details["lon"]); i += 1
            got_geo += 1
        if cx:
            sets.append(f"complex_name=${i}"); params.append(cx); i += 1
            if details.get("complex_url"):
                sets.append(f"complex_url=COALESCE(${i}, complex_url)")
                params.append(details["complex_url"]); i += 1
            got_cx += 1
        if details.get("address_full"):
            sets.append(f"address=CASE WHEN address IS NULL OR btrim(address)='' "
                        f"THEN ${i} ELSE address END")
            params.append(details["address_full"]); i += 1
        if details.get("photos"):
            import json as _json
            sets.append(f"photos=${i}::jsonb")
            params.append(_json.dumps(details["photos"][:10])); i += 1
        params.append(r["id"])
        await execute(
            f"UPDATE rental_listings SET {', '.join(sets)} WHERE id=${i}", *params)

    logger.info("rental backfill: %d checked, координаты %d, ЖК %d",
                report["checked"], got_geo, got_cx)
    report.update(coords=got_geo, complex=got_cx)
    return report
