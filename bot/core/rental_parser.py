"""
Rental index builder.

Парсит объявления об АРЕНДЕ с Krisha.kz:
  /arenda/kvartiry/astana/        → квартиры
  /arenda/kvartiry-posutochno/astana/ → квартиры посуточно
  /arenda/garazhi/astana/        → паркинги/гаражи
  /arenda/kommercheskaya-nedvizhimost/astana/ → коммерческая

После парсинга пересчитывает rental_index:
  медиана/среднее/p25/p75 по (город, район, ЖК, комнаты, тип).

Запуск: await run_rental_cycle()

Period paths verified in a normal browser on 2026-10-15 22:36 UTC+5:
monthly switch/heading and first 23 cards said «за месяц»; daily switch led
to /arenda/kvartiry-posutochno/astana/ and its first 23 cards said «за сутки».
These are bounded source samples, not proof of the entire catalog's period.
Explicit contradictory card text remains unknown; prices never imply period.
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
import asyncpg
from urllib.parse import parse_qs, urlparse
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup
from bot.core.rental_period import classify_rental_period, rental_period_conflict, catalog_period, catalog_cursor_key, catalog_page_url
from bot.core.rental_stage0_identity import RentalStage0MigrationRequired, resolve_rental_complex, observed_hex, source_complex_url

logger = logging.getLogger(__name__)

BASE_URL = "https://krisha.kz"

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9",
}

# path → prop_type
RENTAL_PATHS: dict[str, str] = {
    "/arenda/kvartiry/astana/":       "apartment",
    "/arenda/kvartiry-posutochno/astana/": "apartment",
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
    rent_period: str = "unknown"
    complex_id: int | None = None
    rent_period_conflict: bool = False


def _extract_int(text: str) -> int | None:
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def _extract_rooms(title: str) -> int | None:
    m = re.search(r"(?<!\d)(\d{1,2})\s*-?\s*комн", title.lower())
    if m:
        return int(m.group(1))
    if "студия" in title.lower():
        return 0
    return None


def _extract_area(text: str) -> float | None:
    m = re.search(r"(?<![\d/])(\d{1,3}(?:[ \u00a0]\d{3})*(?:[.,]\d+)?|\d+(?:[.,]\d+)?)\s*м(?:²|2)", text)
    if not m:
        return None
    try:
        return float(re.sub(r"[\s\u00a0]", "", m.group(1)).replace(",", "."))
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
        if (parsed.scheme != "https" or parsed.hostname not in ("astps-photos-kr.kcdn.kz", "krisha-photos.kcdn.online")
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


def _parse_card(card, prop_type: str, *, verified_catalog_period: str = "unknown") -> RentalListing | None:
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
        floor_text = title + " " + (descr_el.get_text(" ", strip=True) if descr_el else "")
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
            rent_period=classify_rental_period(price_text, title, verified_catalog_period=verified_catalog_period),
            rent_period_conflict=rental_period_conflict(price_text, title, verified_catalog_period=verified_catalog_period),
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
        listing = _parse_card(card, prop_type, verified_catalog_period=catalog_period(url))
        if listing:
            results[listing.id] = listing
    return RentalPage(list(results.values()), tuple(card_ids), last_page,
                      has_next, last_confirmed, total_count)


async def _fetch_page(client: httpx.AsyncClient, url: str, prop_type: str) -> list[RentalListing]:
    """Compatibility wrapper. Fetch failures propagate; they are not EOF."""
    return (await _fetch_page_result(client, url, prop_type)).listings


async def collect_rental_page(client: httpx.AsyncClient, path: str, prop_type: str,
                              *, refresh_head: bool = False, max_durable_pages: int = 1,
                              head_min_interval_seconds: float = 0) -> dict:
    """A bounded durable batch, optionally preceded by a fresh-list check.

    A hot page never advances deep coverage. It is reused when the cursor is
    at page one. A validated smaller catalog resets an out-of-range old cursor
    as an aborted round, never a completed one. No detail request is made.
    """
    from bot.db import settings as app_settings
    await app_settings.load()
    key = catalog_cursor_key(prop_type, path)
    cursor = RentalPagination.loads(app_settings.get(key, ""))
    max_durable_pages = max(1, min(8, int(max_durable_pages)))
    head_min_interval_seconds = max(0., min(2700., float(head_min_interval_seconds)))
    metrics = {"requests": 0, "head_pages": 0, "durable_pages": 0,
               "saved": 0, "new": 0, "updated": 0, "reactivated": 0,
               "raw_cards": 0, "accepted_cards": 0, "unpriced_cards": 0}
    reconciled = False

    async def fetch_page(page: int) -> RentalPage:
        metrics["requests"] += 1
        url = catalog_page_url(path, page)
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
        last_head = _rental_index_datetime(app_settings.get(f"{key}_HEAD_CHECKED_AT", ""))
        head_due = (last_head is None or last_head > datetime.now(timezone.utc)
                    or (datetime.now(timezone.utc) - last_head).total_seconds()
                    >= max(0, head_min_interval_seconds))
        head = await fetch_page(1) if refresh_head and head_due else None
        if head is not None:
            await save_page(head)
            metrics["head_pages"] = 1
            await app_settings.set(f"{key}_HEAD_CHECKED_AT", datetime.now(timezone.utc).isoformat())
            if head.last_page_confirmed and head.last_page and cursor.page > head.last_page:
                reconciled = True
                await app_settings.set(f"{key}_ABORTED_AT", datetime.now(timezone.utc).isoformat())
                await app_settings.set(f"{key}_ABORT_REASON", "cursor_beyond_confirmed_catalog_end")
                cursor = RentalPagination()
                await app_settings.set(key, cursor.dumps())
            if head.last_page_confirmed:
                cursor.expected_last_page = head.last_page
                await app_settings.set(f"{key}_EXPECTED_LAST_PAGE", str(head.last_page or 0))
            if head.total_count is not None:
                cursor.expected_total = head.total_count
                await app_settings.set(f"{key}_EXPECTED_TOTAL", str(head.total_count))

        ended = completed = False
        for batch_page in range(max_durable_pages):
            page = cursor.page
            if page == 1 and head is not None and batch_page == 0:
                result = head
            else:
                if head is not None or batch_page:
                    await asyncio.sleep(random.uniform(MIN_SLEEP, MAX_SLEEP))
                result = await fetch_page(page)
                await save_page(result)
                if page == 1:
                    await app_settings.set(f"{key}_HEAD_CHECKED_AT", datetime.now(timezone.utc).isoformat())
            now = datetime.now(timezone.utc)
            if page == 1 and cursor.pages_seen == 0:
                cursor.started_from_first = True
                await app_settings.set(f"{key}_STARTED_AT", now.isoformat())
            if head is not None and head.last_page_confirmed:
                cursor.expected_last_page = head.last_page
            if head is not None and head.total_count is not None:
                cursor.expected_total = head.total_count
            ended = cursor.advance(result)
            completed = ended and cursor.started_from_first and cursor.pages_seen >= page
            # Commit every successful page. Failure/cancellation on page N+1
            # must never lose the already saved coverage of pages 1..N.
            if not ended:
                await app_settings.set(key, cursor.dumps())
                metrics["durable_pages"] += 1
                await app_settings.set(f"{key}_LAST_SUCCESS_AT", now.isoformat())
            if ended:
                break
        # Old cursors only contain signatures/page. Finishing their tail does
        # not prove pages 1..N were scanned successfully in this deployment.
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
            metrics["durable_pages"] += 1
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
        metrics.update(expected_last_page=cursor.expected_last_page,
                       expected_total=cursor.expected_total)
        exc.rental_metrics = metrics
        raise


async def parse_rental_path(path: str, prop_type: str, max_pages: int | None = MAX_PAGES_PER_PATH) -> list[RentalListing]:
    """Unbounded by default; an explicit positive limit is for partial scans."""
    all_listings: dict[str, RentalListing] = {}
    cursor = RentalPagination()
    async with httpx.AsyncClient(follow_redirects=True) as client:
        while not max_pages or cursor.page <= max_pages:
            page = cursor.page
            url = catalog_page_url(path, page)
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
            cid = await resolve_rental_complex(complex_url=listing.complex_url, complex_name=listing.complex_name,
                                               address=listing.address, title=listing.title)
            outcome = await fetchrow(
                """
                WITH previous AS MATERIALIZED (
                    SELECT id, price, is_active, rent_period, complex_id, hex_id, hex_edge_m, rent_period_conflict FROM rental_listings
                     WHERE id = $1 FOR UPDATE
                ), observed AS (
                    INSERT INTO rental_listings
                        (id, url, title, price, area, rooms, floor, floors_total,
                         address, district, complex_name, city, prop_type,
                         published_at, found_at, last_seen, complex_url, photo_url, rent_period, complex_id, rent_period_conflict,
                         rental_episode_started_at,rental_episode_last_seen_at)
                    SELECT $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15::timestamptz,$15::timestamptz,
                           $16,$17,$18::text,$19,$20::boolean,
                           CASE WHEN $18::text IN ('monthly','daily') AND NOT $20::boolean THEN $15::timestamptz ELSE NULL END,
                           CASE WHEN $18::text IN ('monthly','daily') AND NOT $20::boolean THEN $15::timestamptz ELSE NULL END
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
                        rent_period = CASE WHEN rental_listings.rent_period_conflict OR EXCLUDED.rent_period_conflict
                            OR (rental_listings.rent_period IN ('daily','monthly')
                            AND EXCLUDED.rent_period IN ('daily','monthly')
                            AND rental_listings.rent_period <> EXCLUDED.rent_period) THEN 'unknown'
                            ELSE EXCLUDED.rent_period END,
                        rent_period_conflict = rental_listings.rent_period_conflict OR EXCLUDED.rent_period_conflict
                            OR (rental_listings.rent_period IN ('daily','monthly')
                            AND EXCLUDED.rent_period IN ('daily','monthly')
                            AND rental_listings.rent_period <> EXCLUDED.rent_period),
                        complex_id = COALESCE(rental_listings.complex_id, EXCLUDED.complex_id),
                        rental_episode_started_at = CASE
                            WHEN rental_listings.rent_period_conflict OR EXCLUDED.rent_period_conflict
                              OR EXCLUDED.rent_period NOT IN ('monthly','daily')
                              OR (rental_listings.rent_period IN ('monthly','daily')
                                  AND rental_listings.rent_period <> EXCLUDED.rent_period) THEN NULL
                            WHEN rental_listings.rent_period=EXCLUDED.rent_period
                              AND rental_listings.is_active IS TRUE AND rental_listings.archived_at IS NULL
                              AND rental_listings.rental_episode_started_at IS NOT NULL
                              AND rental_listings.rental_episode_started_at<=EXCLUDED.last_seen
                              AND rental_listings.rental_episode_last_seen_at
                                  BETWEEN EXCLUDED.last_seen-interval '72 hours' AND EXCLUDED.last_seen
                            THEN rental_listings.rental_episode_started_at ELSE EXCLUDED.last_seen END,
                        rental_episode_last_seen_at = CASE
                            WHEN rental_listings.rent_period_conflict OR EXCLUDED.rent_period_conflict
                              OR EXCLUDED.rent_period NOT IN ('monthly','daily')
                              OR (rental_listings.rent_period IN ('monthly','daily')
                                  AND rental_listings.rent_period <> EXCLUDED.rent_period) THEN NULL
                            ELSE EXCLUDED.last_seen END,
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
                    RETURNING rental_listings.*, (xmax = 0) AS is_new
                ), changed AS (
                    INSERT INTO rental_price_history (listing_id, old_price, new_price)
                    SELECT observed.id, previous.price, observed.price
                      FROM observed JOIN previous USING (id)
                     WHERE previous.price > 0 AND observed.price > 0
                       AND previous.rent_period = $18 AND $18 IN ('daily','monthly')
                       AND NOT previous.rent_period_conflict AND NOT $20
                       AND previous.price <> observed.price
                    RETURNING listing_id
                ), captured AS (
                    INSERT INTO rental_observations
                      (listing_id,observed_at,observed_on,source,rent_period,period_source_checked,episode_started_at,
                       city,district,rooms,price,area,complex_name,complex_url,complex_id,
                       hex_id,hex_edge_m,furnishing,furnishing_version)
                    SELECT o.id,$15::timestamptz,($15::timestamptz AT TIME ZONE 'UTC')::date,'krisha',o.rent_period,TRUE,o.rental_episode_started_at,
                           o.city,o.district,o.rooms,o.price,o.area,o.complex_name,o.complex_url,o.complex_id,
                           o.hex_id,o.hex_edge_m,o.furnishing,o.furnishing_version
                    FROM observed o WHERE o.city='astana' AND o.prop_type='apartment'
                      AND o.rent_period IN ('monthly','daily') AND NOT o.rent_period_conflict
                      AND o.rental_episode_started_at IS NOT NULL
                      AND NOT coalesce(o.is_duplicate,FALSE) AND NOT coalesce(o.dup_needs_review,FALSE)
                      AND nullif(btrim(o.duplicate_of),'') IS NULL AND o.rooms BETWEEN 1 AND 6 AND o.price>0
                    ON CONFLICT(listing_id,observed_on,source,rent_period) DO NOTHING
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
                listing.rent_period, cid, listing.rent_period_conflict,
            )
            if outcome is None:
                raise RuntimeError("Rental upsert returned no outcome")
            saved += 1
            new += int(outcome["is_new"])
            updated += int(not outcome["is_new"])
            reactivated += int(outcome["reactivated"])
        except (asyncpg.UndefinedColumnError, asyncpg.UndefinedTableError) as exc:
            from bot.core.rental_furnishing import RentalStage2MigrationRequired
            raise RentalStage2MigrationRequired() from exc
        except Exception as exc:
            logger.warning("Save error %s: %s", listing.id, type(exc).__name__)
    return RentalSaveCount(saved, new=new, updated=updated, reactivated=reactivated)


RENTAL_INDEX_POLICY_VERSION = "3"
RENTAL_INDEX_WINDOW_DAYS = 30
RENTAL_INDEX_TTL_HOURS = 72

# A source URL may identify an umbrella, but cannot identify one of its houses
# by proximity. An explicit house FK comes from the address/token resolver;
# only that FK may inherit the parent's source link. A name-only fallback must
# agree with the source identity of the exact non-umbrella canonical project.
_RENTAL_INDEX_ROWS_SQL = """
WITH cx AS (
    SELECT id,coalesce(canonical_id,id) AS cid,complex_name_key(name) AS n
    FROM complexes WHERE NOT coalesce(is_garbage,false)
      AND NOT coalesce(is_street,false)
      AND canonical_reason IS DISTINCT FROM 'junk_unmatched'
), names AS (
    SELECT n,cid FROM cx
    UNION SELECT a.normalized_name,cx.cid FROM complex_aliases a
      JOIN cx ON cx.id=a.complex_id WHERE a.status='verified'
), unique_names AS (
    SELECT n,min(cid) AS cid FROM names WHERE n<>''
    GROUP BY n HAVING count(DISTINCT cid)=1
), source_links AS (
    SELECT rtrim(split_part(split_part(c.krisha_url,'?',1),'#',1),'/') AS url,cx.cid
    FROM complexes c JOIN cx ON cx.id=c.id WHERE c.krisha_url IS NOT NULL
    UNION SELECT rtrim(split_part(split_part(sl.url,'?',1),'#',1),'/'),cx.cid
    FROM complex_source_links sl JOIN cx ON cx.id=sl.complex_id
    WHERE sl.source='krisha' AND (sl.match_method='manual' OR sl.confidence>=0.85)
), unique_sources AS (
    SELECT url,min(cid) AS cid FROM source_links GROUP BY url
    HAVING count(DISTINCT cid)=1
)
SELECT r.id,r.city,r.district,r.complex_name,r.complex_url,r.complex_id,
       r.rooms,r.prop_type,r.rent_period,r.rent_period_conflict,r.price,r.area,r.title,r.last_seen,
       r.is_active,r.archived_at,r.is_duplicate,r.dup_needs_review,r.duplicate_of,
       identity.id AS index_complex_id,identity.name AS index_complex_name,
       (identity.id IS NOT NULL) AS complex_binding_verified
FROM rental_listings r
LEFT JOIN cx bound ON bound.id=r.complex_id
LEFT JOIN unique_names named ON r.complex_id IS NULL
  AND named.n=complex_name_key(r.complex_name)
LEFT JOIN unique_sources source ON r.complex_url ~ '^https://krisha[.]kz/complex/show/[^/?#]+(/[^/?#]+)?/?$'
  AND source.url=rtrim(r.complex_url,'/')
LEFT JOIN complexes identity ON identity.id=coalesce(bound.cid,named.cid)
  AND NOT coalesce(identity.is_garbage,false) AND NOT coalesce(identity.is_street,false)
  AND NOT coalesce(identity.is_umbrella,false)
  AND identity.canonical_reason IS DISTINCT FROM 'junk_unmatched'
  AND (source.cid=identity.id OR
       (r.complex_id IS NOT NULL AND source.cid=identity.parent_complex_id))
  AND NOT EXISTS (
    SELECT 1 FROM names conflicting WHERE conflicting.n=complex_name_key(r.complex_name)
      AND conflicting.cid<>identity.id
      AND conflicting.cid IS DISTINCT FROM identity.parent_complex_id
  )
WHERE r.last_seen BETWEEN $1::timestamptz - INTERVAL '72 hours' AND $1
  AND r.city = 'astana' AND r.prop_type = 'apartment' AND r.rent_period = 'monthly'
  AND NOT coalesce(r.rent_period_conflict,false)
  AND r.is_active IS TRUE AND r.archived_at IS NULL
  AND NOT coalesce(r.is_duplicate,false) AND NOT coalesce(r.dup_needs_review,false)
  AND nullif(btrim(r.duplicate_of),'') IS NULL
  AND r.price BETWEEN 50000 AND 2000000 AND r.area BETWEEN 20 AND 300
  AND r.rooms BETWEEN 1 AND 6
ORDER BY r.last_seen DESC,r.id
"""


def _rental_index_thresholds() -> tuple[int, int]:
    from bot.db import settings
    def threshold(key, minimum):
        try:
            return max(minimum, min(10000, settings.get_int(key, minimum)))
        except (ValueError, TypeError, OverflowError):
            return minimum
    return threshold("RENT_MIN_SAMPLE_COMPLEX", 5), threshold("RENT_MIN_SAMPLE_AREA", 10)


def _rental_index_datetime(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, OverflowError):
            return None
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    try:
        return value.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _rental_index_records(rows, rebuilt_at, min_complex=5, min_area=10):
    """Fresh monthly asking aggregates, with distinct IDs and source boundaries.

    Inclusive quartiles interpolate inside the observed endpoints (the same
    empirical convention as the public rental estimator). The observed
    sample is the distribution being described, not a population forecast.
    The nominal 30-day window never relaxes the 72-hour source freshness gate.
    """
    import math
    from datetime import timedelta
    from bot.core.comparison_rental_estimate import _NONMONTHLY

    groups = defaultdict(list)
    seen = set()
    # Newest observation wins if a source ID was accidentally repeated.
    ordered = sorted(rows, key=lambda row: _rental_index_datetime(row.get("last_seen"))
                     or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    for row in ordered:
        identifier = row.get("id")
        observed = _rental_index_datetime(row.get("last_seen"))
        price, area, rooms = row.get("price"), row.get("area"), row.get("rooms")
        if (not isinstance(identifier, str) or not identifier or identifier in seen
                or row.get("city") != "astana" or row.get("prop_type") != "apartment"
                or row.get("rent_period") != "monthly" or row.get("rent_period_conflict") not in (False, None)
                or row.get("is_active") is not True
                or row.get("archived_at") is not None or row.get("is_duplicate") not in (False, None)
                or row.get("dup_needs_review") not in (False, None) or row.get("duplicate_of")
                or observed is None or not rebuilt_at-timedelta(hours=72) <= observed <= rebuilt_at
                or type(price) is not int or not 50000 <= price <= 2000000
                or type(area) not in (int, float) or not math.isfinite(area) or not 20 <= area <= 300
                or type(rooms) is not int or not 1 <= rooms <= 6
                or _NONMONTHLY.search(row.get("title") or "")):
            continue
        seen.add(identifier)
        district = (row.get("district") or "").strip() or None
        cid, name = row.get("index_complex_id"), row.get("index_complex_name")
        if (row.get("complex_binding_verified") is not True or type(cid) is not int or cid <= 0
                or not isinstance(name, str) or not name.strip()):
            cid, name = None, None
        else:
            name = name.strip()
        keys = {("astana", None, None, None, rooms, "apartment"),
                ("astana", None, None, None, None, "apartment")}
        if district:
            keys.update({("astana", district, None, None, rooms, "apartment"),
                         ("astana", district, None, None, None, "apartment")})
        if cid is not None:
            # A canonical complex is independent of an advertiser's district spelling.
            keys.update({("astana", None, name, cid, rooms, "apartment"),
                         ("astana", None, name, cid, None, "apartment")})
        for key in keys:
            groups[key].append((price, price/area, observed))

    records = []
    for (city, district, name, cid, rooms, prop_type), values in groups.items():
        if len(values) < max(5 if cid is not None else 10, min_complex if cid is not None else min_area):
            continue
        prices = [item[0] for item in values]
        q25, _, q75 = statistics.quantiles(prices, n=4, method="inclusive")
        source_times = [item[2] for item in values]
        records.append((city, district, name, cid, rooms, prop_type, "monthly", None,
                        int(statistics.median(prices)), int(statistics.mean(prices)),
                        int(q25), int(q75), len(prices), int(statistics.median(item[1] for item in values)),
                        rebuilt_at, RENTAL_INDEX_WINDOW_DAYS, len(prices),
                        min(source_times), max(source_times), RENTAL_INDEX_POLICY_VERSION))
    return records


async def rebuild_rental_index() -> None:
    """Replace only fresh monthly apartment aggregates, atomically and serialized.

    Daily/furnished/other-city and other-property indexes belong to their own
    rebuilds and are untouched, including when this scope becomes empty.
    Unverified legacy aggregates in this scope are retired after a successful
    replacement. Their source listings remain available for evidence backfill.
    """
    from bot.db.pg import get_pool
    minimum_complex, minimum_area = _rental_index_thresholds()
    async with get_pool().acquire() as connection:
        async with connection.transaction():
            await connection.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                                     "rental_index:astana:apartment:monthly:all-furnishing")
            rebuilt_at = datetime.now(timezone.utc)
            rows = await connection.fetch(_RENTAL_INDEX_ROWS_SQL, rebuilt_at)
            records = _rental_index_records(rows, rebuilt_at, minimum_complex, minimum_area)
            if records:
                await connection.executemany("""
                    INSERT INTO rental_index
                        (city,district,complex_name,complex_id,rooms,prop_type,rent_period,furnishing,
                         median_price,avg_price,p25_price,p75_price,sample_count,price_per_sqm,
                         updated_at,window_days,active_supply,source_oldest_at,source_newest_at,policy_version)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20)
                    ON CONFLICT ON CONSTRAINT rental_index_key_v2 DO UPDATE SET
                      median_price=EXCLUDED.median_price,avg_price=EXCLUDED.avg_price,
                      p25_price=EXCLUDED.p25_price,p75_price=EXCLUDED.p75_price,
                      sample_count=EXCLUDED.sample_count,price_per_sqm=EXCLUDED.price_per_sqm,
                      updated_at=EXCLUDED.updated_at,window_days=EXCLUDED.window_days,
                      active_supply=EXCLUDED.active_supply,source_oldest_at=EXCLUDED.source_oldest_at,
                      source_newest_at=EXCLUDED.source_newest_at,policy_version=EXCLUDED.policy_version
                """, records)
            await connection.execute("""
                DELETE FROM rental_index WHERE city='astana' AND prop_type='apartment'
                  AND furnishing IS NULL AND (
                    (rent_period='monthly' AND (updated_at IS NULL OR updated_at < $1 OR policy_version IS DISTINCT FROM $2))
                    OR (rent_period='unknown' AND policy_version IS NULL)
                  )
            """, rebuilt_at, RENTAL_INDEX_POLICY_VERSION)
            await connection.execute("""
                INSERT INTO app_settings (key,value,updated_at)
                VALUES ('RENTAL_INDEX_POLICY_VERSION',$1,now())
                ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value,updated_at=now()
            """, RENTAL_INDEX_POLICY_VERSION)
            from bot.core.rental_stage2_index import rebuild_rental_stage2_index
            await rebuild_rental_stage2_index(connection, rebuilt_at)
    logger.info("rental_index rebuilt: %d monthly groups", len(records))


async def lookup_rental_estimate(
    city: str,
    district: str | None,
    complex_name: str | None,
    rooms: int | None,
    prop_type: str = "apartment",
    *, complex_id: int | None = None,
) -> dict | None:
    """
    Найти оценку аренды для объекта.
    Приоритет: ЖК+комнаты → район+комнаты → город+комнаты → район → город.
    """
    from bot.db.pg import fetchrow
    # The apartment category establishes monthly scope. Parking/commercial
    # list cards can mix hourly and per-m² offers; the schema has no unit yet.
    if prop_type != "apartment":
        return None
    district = _extract_district(district or "") or district
    minimum_complex, minimum_area = _rental_index_thresholds()
    now = datetime.now(timezone.utc)
    if complex_id is not None and (type(complex_id) is not int or complex_id <= 0):
        complex_id = None
        complex_name = None

    # ВАЖНО: комнатность сохраняем как можно дольше — аренда однушки,
    # применённая к трёшке, даёт мусорный yield. Порядок:
    # ЖК+комнаты → район+комнаты → город+комнаты → район → город.
    attempts = []
    if complex_id is not None or complex_name:
        attempts.append((city, None, complex_name, rooms, "ЖК", complex_id))
    if district:
        attempts.append((city, district, None, rooms, "район", None))
    attempts.append((city, None, None, rooms, "город", None))
    if district:
        attempts.append((city, district, None, None, "район (все комн.)", None))
    attempts.append((city, None, None, None, "город (все комн.)", None))

    seen = set()
    for (c, d, jk, r, level, cid) in attempts:
        scope = (c, d, jk, r, cid)
        if scope in seen:
            continue
        seen.add(scope)
        minimum = minimum_complex if level == "ЖК" else minimum_area
        row = await fetchrow(
            """
            WITH names AS (
                SELECT c.id,coalesce(c.canonical_id,c.id) AS cid,complex_name_key(c.name) AS n
                FROM complexes c WHERE NOT coalesce(c.is_garbage,false)
                  AND NOT coalesce(c.is_street,false) AND c.canonical_reason IS DISTINCT FROM 'junk_unmatched'
                UNION SELECT c.id,coalesce(c.canonical_id,c.id),a.normalized_name
                FROM complex_aliases a JOIN complexes c ON c.id=a.complex_id
                WHERE a.status='verified' AND NOT coalesce(c.is_garbage,false)
                  AND NOT coalesce(c.is_street,false) AND c.canonical_reason IS DISTINCT FROM 'junk_unmatched'
            ), target AS (
                SELECT min(names.cid) AS cid FROM names JOIN complexes canonical ON canonical.id=names.cid
                WHERE (($6::integer IS NOT NULL AND names.id=$6)
                       OR ($6::integer IS NULL AND $3::text IS NOT NULL AND names.n=complex_name_key($3)))
                  AND NOT coalesce(canonical.is_umbrella,false) AND NOT coalesce(canonical.is_garbage,false)
                  AND NOT coalesce(canonical.is_street,false)
                  AND canonical.canonical_reason IS DISTINCT FROM 'junk_unmatched'
                HAVING count(DISTINCT names.cid)=1
            )
            SELECT i.median_price,i.avg_price,i.p25_price,i.p75_price,i.sample_count,i.price_per_sqm,
                   i.updated_at,i.source_oldest_at,i.source_newest_at,i.window_days,i.policy_version,
                   i.complex_id,i.complex_name,i.rent_period,i.active_supply
            FROM rental_index i
            WHERE i.city=$1 AND i.district IS NOT DISTINCT FROM $2::text
              AND (($6::integer IS NOT NULL OR $3::text IS NOT NULL) AND i.complex_id=(SELECT cid FROM target)
                   OR $6::integer IS NULL AND $3::text IS NULL AND i.complex_id IS NULL AND i.complex_name IS NULL)
              AND i.rooms IS NOT DISTINCT FROM $4::integer AND i.prop_type=$5
              AND i.rent_period='monthly' AND i.furnishing IS NULL
              AND i.updated_at BETWEEN $8::timestamptz - INTERVAL '72 hours' AND $8
              AND i.source_oldest_at BETWEEN $8::timestamptz - INTERVAL '72 hours' AND i.source_newest_at
              AND i.source_newest_at BETWEEN i.source_oldest_at AND i.updated_at
              AND i.sample_count >= $7 AND i.window_days=30 AND i.policy_version=$9
              AND i.p25_price>0 AND i.p25_price<=i.median_price AND i.median_price<=i.p75_price
              AND EXISTS (SELECT 1 FROM app_settings WHERE key='RENTAL_INDEX_POLICY_VERSION' AND value=$9)
            LIMIT 1
            """,
            c, d, jk, r, prop_type, cid, minimum, now, RENTAL_INDEX_POLICY_VERSION,
        )
        if row:
            result = dict(row)
            if not _rental_index_result_ready(result, now, minimum):
                continue
            result["level"] = level
            return result
    return None


def _rental_index_result_ready(row, now, minimum):
    """Fail closed on old-policy rows or missing/stale source provenance."""
    from datetime import timedelta
    updated, oldest, newest = (_rental_index_datetime(row.get(field)) for field in
                               ("updated_at", "source_oldest_at", "source_newest_at"))
    count = row.get("sample_count")
    prices = [row.get(field) for field in ("p25_price", "median_price", "p75_price")]
    return (row.get("policy_version") == RENTAL_INDEX_POLICY_VERSION
            and row.get("rent_period") == "monthly" and row.get("window_days") == RENTAL_INDEX_WINDOW_DAYS
            and type(count) is int and count >= minimum
            and all(type(price) is int and 50000 <= price <= 2000000 for price in prices)
            and prices[0] <= prices[1] <= prices[2]
            and None not in (updated, oldest, newest)
            and now-timedelta(hours=RENTAL_INDEX_TTL_HOURS) <= oldest <= newest <= updated <= now)


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

def _rental_description_is_fresh(checked_at, *, now=None):
    """A rule/grid update cannot refresh the age of a source description."""
    from datetime import timedelta
    checked = _rental_index_datetime(checked_at)
    now = now or datetime.now(timezone.utc)
    return checked is not None and now-timedelta(hours=72) <= checked <= now


async def backfill_rental_details(batch: int = 8, *, budget_seconds: float = 30,
                                  progress: dict | None = None,
                                  prop_type: str | None = None,
                                  rent_period: str | None = None) -> dict:
    """Докачивает детальные страницы аренды: у каждого объявления должна
    быть привязка — ЖК (официальный блок map.complex), координаты
    (JS-блоб страницы) или хотя бы адрес. Берёт только живые объявления."""
    import random as _rnd
    from bot.db.pg import fetch, execute

    from bot.core.apartment_details import (fetch_apartment_details, ListingBlockedError,
                                           detail_fetch_cooldown_remaining, REQUEST_COUNTS)
    report = {} if progress is None else progress
    report.update(checked=0, coords=0, complex=0, requests=0, http_468=0,
                  detail_attempts=0, success=0, selected=0, blocked=0, errors=0,
                  budget_exhausted=False, status="idle", cooldown_remaining_seconds=0,
                  queue_scope=("apartment_daily" if rent_period == "daily" else prop_type or "all"))
    if prop_type not in (None, "apartment", "parking", "commercial") or rent_period not in (None, "monthly", "daily"):
        raise ValueError("Invalid rental detail scope")
    if batch <= 0 or budget_seconds <= 0:
        return report
    cooldown = detail_fetch_cooldown_remaining()
    if cooldown:
        report["cooldown"] = True
        report.update(status="cooldown", cooldown_remaining_seconds=round(cooldown, 3))
        return report
    deadline = time.monotonic() + budget_seconds
    from bot.db import settings as app_settings
    from bot.core.rental_furnishing import classify_furnishing, compile_custom_rules, version_tag
    raw_furnishing_rules = app_settings.get('RENTAL_FURNISHING_RULES_JSON', '{}')
    furnishing_rules = compile_custom_rules(raw_furnishing_rules)
    furnishing_version = version_tag(raw_furnishing_rules)
    edge_m = app_settings.get("HEX_EDGE_M", "50")
    edge_probe = observed_hex(51.128, 71.430, edge_m)
    expected_edge = edge_probe["hex_edge_m"] if edge_probe else None

    try:
        rows = await fetch("""
        SELECT id, url, lat, lon, complex_name, complex_url, address, title,
               complex_id, hex_id, hex_edge_m, rent_period, rent_period_conflict,
               description, description_checked_at, furnishing_version,
               count(*) OVER () AS queue_due,
               max(extract(epoch FROM now()-COALESCE(description_checked_at,found_at))/3600)
                   OVER () AS oldest_due_age_hours FROM rental_listings
        WHERE (lat IS NULL OR complex_name IS NULL OR btrim(complex_name) = ''
               OR complex_url IS NULL OR btrim(complex_url) = '' OR complex_id IS NULL
               OR hex_id IS NULL OR hex_edge_m IS NULL
               OR description_checked_at IS NULL
               OR description_checked_at < now() - interval '72 hours'
               OR description_checked_at > now()
               OR (description_checked_at IS NOT NULL AND furnishing_version IS DISTINCT FROM $3::text)
               OR ($2::real IS NOT NULL AND hex_edge_m IS DISTINCT FROM $2))
          AND is_active IS NOT FALSE
          AND archived_at IS NULL
          AND city = 'astana'
          AND ($4::text IS NULL OR prop_type = $4)
          AND ($5::text IS NULL OR rent_period = $5)
          AND COALESCE(rent_period_conflict, FALSE) = FALSE
          AND COALESCE(is_duplicate, FALSE) = FALSE
          AND COALESCE(dup_needs_review, FALSE) = FALSE
          AND NULLIF(btrim(duplicate_of),'') IS NULL
          AND url IS NOT NULL
          AND last_seen > now() - interval '7 days'
          AND (coord_fetch_attempted_at IS NULL
               OR coord_fetch_attempted_at < now() - interval '3 days')
        ORDER BY coord_fetch_attempted_at ASC NULLS FIRST, found_at ASC NULLS FIRST, id
        LIMIT $1
        """, batch, expected_edge, furnishing_version, prop_type, rent_period)
    except asyncpg.UndefinedColumnError as exc:
        from bot.core.rental_furnishing import RentalStage2MigrationRequired
        raise RentalStage2MigrationRequired() from exc
    if not rows:
        report["eligible_due"] = 0
        return report
    report["selected"] = len(rows)
    report["eligible_due"] = int(rows[0].get("queue_due") or len(rows))
    age = rows[0].get("oldest_due_age_hours")
    report["oldest_due_age_hours"] = round(max(0, float(age)), 3) if age is not None else None

    # Detail fetches use their own cooldown; list collection never consults it.
    from bot.core.complex_audit import street_names
    streets = await street_names()

    got_geo = got_cx = 0
    for r in rows:
        description_fresh = _rental_description_is_fresh(r.get('description_checked_at'))
        if description_fresh and r.get('furnishing_version') != furnishing_version:
            await execute('UPDATE rental_listings SET furnishing=$2,furnishing_version=$3 WHERE id=$1',
                          r['id'], classify_furnishing(r.get('title'),r.get('description'),furnishing_rules),furnishing_version)
            if (r.get('lat') is not None and r.get('lon') is not None and r.get('complex_id')
                    and r.get('complex_url') and r.get('hex_id') and r.get('hex_edge_m') == expected_edge):
                continue
        # Existing source coordinates can acquire their grid projection
        # without another source request. This is never a complex guess.
        known_hex = observed_hex(r.get("lat"), r.get("lon"), edge_m)
        if known_hex and (r.get("hex_id") != known_hex["hex_id"] or r.get("hex_edge_m") != known_hex["hex_edge_m"]):
            await execute("UPDATE rental_listings SET hex_id=$2,hex_edge_m=$3 WHERE id=$1",
                          r["id"], known_hex["hex_id"], known_hex["hex_edge_m"])
            if (r.get("complex_name") and r.get("complex_url") and r.get("complex_id")
                    and description_fresh):
                continue
        if detail_fetch_cooldown_remaining():
            report["cooldown"] = True
            break
        pause = _rnd.uniform(8.0, 15.0)
        remaining = deadline - time.monotonic()
        # Leave enough time for the shared fetcher's own 3–6 s pacing.
        if remaining <= pause + 7:
            report["budget_exhausted"] = True
            break
        await asyncio.sleep(pause)
        if detail_fetch_cooldown_remaining():
            report["cooldown"] = True
            break
        before_requests = REQUEST_COUNTS["detail"]
        report["detail_attempts"] += 1
        # Reserve the fetcher's unchanged <=6s pacing plus bookkeeping. The
        # actual HTTP timeout is explicit, never silently a 30s call inside a
        # shorter maintenance deadline.
        request_timeout = min(30., max(.5, deadline - time.monotonic() - 7.))
        report["request_timeout_seconds"] = round(request_timeout, 3)
        outcome = "error"
        try:
            details = await asyncio.wait_for(
                fetch_apartment_details(r["url"], raise_on_error=True,
                    request_timeout_seconds=request_timeout),
                timeout=max(0.1, deadline - time.monotonic()))
        except ListingBlockedError as exc:
            report["blocked"] += 1
            report["http_468"] += int(str(exc).startswith("468 "))
            outcome = "blocked"
            report["status"] = "blocked"
            break  # only this genuinely attempted row is marked below
        except asyncio.TimeoutError:
            report["errors"] += 1
            report["budget_exhausted"] = True
            outcome = "timeout"
            break
        except asyncio.CancelledError:
            outcome = "cancelled"
            report.update(status="cycle_stopped", budget_exhausted=True)
            raise
        except Exception as e:
            logger.warning("rental backfill fetch failed %s: %s", r["id"], e)
            report["errors"] += 1
            details = None
        finally:
            actual_requests = max(0, REQUEST_COUNTS["detail"] - before_requests)
            report["requests"] += actual_requests
            # Rotate a genuinely attempted source failure instead of keeping
            # the same never-attempted head forever. No freshness timestamp,
            # last_seen or unvisited record is touched here.
            if actual_requests and outcome in ("blocked", "timeout", "cancelled"):
                await execute("UPDATE rental_listings SET coord_fetch_attempted_at=now() WHERE id=$1", r["id"])
        report["checked"] += 1
        if not details or details.get("source_detail_valid") is not True or details.get("is_archived"):
            report["errors"] += int(bool(details))
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
        description = details.get('description')
        description = description.strip()[:10000] if isinstance(description, str) else None
        for field, value in (
            ('description', description),
            ('furnishing', classify_furnishing(r.get('title'), description, furnishing_rules)),
            ('furnishing_version', furnishing_version),
        ):
            sets.append(f'{field}=${i}'); params.append(value); i += 1
        sets.append('description_checked_at=now()')
        projected = observed_hex(details.get("lat"), details.get("lon"), edge_m)
        if projected:
            for field in ("lat", "lon", "hex_id", "hex_edge_m"):
                sets.append(f"{field}=${i}"); params.append(projected[field]); i += 1
            got_geo += 1
        decision = await resolve_rental_complex(
            complex_url=details.get("complex_url") if cx else None, complex_name=cx,
            address=details.get("address_full") or r.get("address"), title=r.get("title"),
            return_decision=True)
        cid = decision["complex_id"]
        # The freshly observed source supersedes old identity, including an
        # unresolved/conflicting binding. Never retain a disproved FK.
        for field, value in (("complex_id", cid), ("complex_name", cx),
                             ("complex_url", source_complex_url(details.get("complex_url")) if cx else None)):
            sets.append(f"{field}=${i}"); params.append(value); i += 1
        if cid:
            got_cx += 1
        if details.get("address_full"):
            sets.append(f"address=${i}")
            params.append(details["address_full"]); i += 1
        if details.get("photos"):
            import json as _json
            sets.append(f"photos=${i}::jsonb")
            params.append(_json.dumps(details["photos"][:10])); i += 1
        params.append(r["id"])
        await execute(
            f"UPDATE rental_listings SET {', '.join(sets)} WHERE id=${i}", *params)
        report["success"] += 1

    logger.info("rental backfill: %d checked, координаты %d, ЖК %d",
                report["checked"], got_geo, got_cx)
    report.update(coords=got_geo, complex=got_cx)
    report["cooldown_remaining_seconds"] = round(detail_fetch_cooldown_remaining(), 3)
    if report["status"] != "blocked":
        report["status"] = ("cooldown" if report.get("cooldown") else
            "budget_exhausted" if report["budget_exhausted"] else
            "success" if report["success"] else "error" if report["errors"] else "idle")
    return report
