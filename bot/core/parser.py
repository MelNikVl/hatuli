"""
Krisha.kz listing parser.

Migrated from krisha_bot/parser.py — uses bot.config.Config instead of Settings,
and adds optional district filtering via das[district][0]=N URL parameter.

District IDs (verify in browser: open krisha.kz → filter by district → copy URL):
  Astana:  Есиль=18, Алматы р-н=14, Сарыарка=17, Байконур=15, Нура=16
  Almaty:  Алмалинский=6, Бостандыкский=3, Ауэзовский=4, Медеуский=5,
           Турксибский=8, Жетысуский=7
These are approximate — confirm by filtering on the live site.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import traceback
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import httpx
from bs4 import BeautifulSoup, Tag

if TYPE_CHECKING:
    from bot.config import Config
    from bot.db.compat import BotDB

logger = logging.getLogger(__name__)

BASE_URL = "https://krisha.kz"
RENT_LISTINGS_PATH = "/arenda/kvartiry/{city}/"
BUY_LISTINGS_PATH = "/prodazha/kvartiry/{city}/"
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
}

# District name → krisha.kz district ID.
# Verify by opening krisha.kz, applying the district filter, and reading
# das[district][0]=N from the URL query string.
ASTANA_DISTRICT_IDS: dict[str, int] = {
    "есиль": 18,
    "алматы р-н": 14,
    "алматинский": 14,
    "сарыарка": 17,
    "байконур": 15,
    "нура": 16,
}

ALMATY_DISTRICT_IDS: dict[str, int] = {
    "алмалинский": 6,
    "бостандыкский": 3,
    "ауэзовский": 4,
    "медеуский": 5,
    "турксибский": 8,
    "жетысуский": 7,
}

_CITY_DISTRICTS: dict[str, dict[str, int]] = {
    "astana": ASTANA_DISTRICT_IDS,
    "almaty": ALMATY_DISTRICT_IDS,
}


def _resolve_district_id(city: str, district_name: str | None) -> int | None:
    """Return the krisha.kz numeric ID for *district_name* in *city*, or None."""
    if not district_name:
        return None
    city_map = _CITY_DISTRICTS.get(city.lower().strip())
    if not city_map:
        return None
    needle = district_name.lower().strip()
    # Exact match first
    if needle in city_map:
        return city_map[needle]
    # Partial match (the user typed "есиль" and map key is "есиль")
    for key, district_id in city_map.items():
        if key in needle or needle in key:
            return district_id
    return None


@dataclass(slots=True)
class Listing:
    id: str
    title: str
    price: int
    address: str
    district: str
    rooms: int | None
    photo_url: str | None
    url: str
    published_at: str
    photo_urls: list = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict compatible with bot.core.cards.send_listing_card."""
        return {
            "id": self.id,
            "title": self.title,
            "price": self.price,
            "address": self.address,
            "district": self.district or None,
            "rooms": self.rooms,
            "photo_url": self.photo_url,
            "photo_urls": self.photo_urls,
            "url": self.url,
            "published_at": self.published_at,
            "source": "krisha.kz",
        }


def _normalize_deal_type(deal_type: str) -> str:
    normalized = deal_type.lower().strip()
    if normalized in {"buy", "sale", "sell", "prodazha"}:
        return "buy"
    return "rent"


def _validate_response_scope(final_url: str, city: str, deal_type: str) -> bool:
    lowered = final_url.lower()
    city_ok = f"/{city.lower()}/" in lowered
    deal_ok = "/prodazha/" in lowered if _normalize_deal_type(deal_type) == "buy" else "/arenda/" in lowered
    return city_ok and deal_ok


def _extract_price(value: str) -> int | None:
    digits = re.sub(r"[^\d]", "", value)
    return int(digits) if digits else None


def _extract_rooms(title: str) -> int | None:
    match = re.search(r"(\d+)\s*-?\s*ком", title.lower())
    if match:
        return int(match.group(1))
    return None


def _extract_listing_id(card: Tag, listing_url: str | None) -> str | None:
    for attr in ("data-id", "data-advert-id", "id"):
        value = card.get(attr)
        if value:
            return str(value)
    if listing_url:
        match = re.search(r"/(\d{5,})", listing_url)
        if match:
            return match.group(1)
    return None


def _full_url(href: str | None) -> str | None:
    if not href:
        return None
    if href.startswith("http"):
        return href
    return f"{BASE_URL}{href}"


def _extract_photo_urls(card: Tag) -> list[str]:
    """Extract up to 5 photo URLs from a listing card."""
    photo_urls: list[str] = []

    # Try JSON-encoded photo list in data attributes first
    for attr in ("data-photos", "data-photo", "data-images"):
        raw = card.get(attr)
        if raw:
            try:
                import json
                photos_list = json.loads(raw)
                if isinstance(photos_list, list):
                    for p in photos_list:
                        if isinstance(p, str):
                            full = _full_url(p)
                        elif isinstance(p, dict):
                            src = p.get("src") or p.get("url") or p.get("href") or ""
                            full = _full_url(src) if src else None
                        else:
                            continue
                        if full and len(photo_urls) < 5:
                            photo_urls.append(full)
            except Exception:
                pass
            if photo_urls:
                break

    if photo_urls:
        return photo_urls

    # Fall back to scraping all <img> tags in the card
    _skip_keywords = ("placeholder", "noimage", "nophoto", "no-photo", "sprite", "icon", "logo")
    for img in card.select("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-lazy-src") or img.get("data-original")
        if not src:
            continue
        src_lower = src.lower()
        if any(kw in src_lower for kw in _skip_keywords):
            continue
        full = _full_url(src)
        if full and full not in photo_urls:
            photo_urls.append(full)
        if len(photo_urls) >= 5:
            break

    return photo_urls


def _parse_card(card: Tag) -> Listing | None:
    link_tag = card.select_one("a.a-card__title") or card.select_one("a[href*='/a/show/']")
    listing_url = _full_url(link_tag.get("href") if link_tag else None)
    listing_id = _extract_listing_id(card, listing_url)

    title = ""
    if link_tag:
        title = " ".join(link_tag.get_text(" ", strip=True).split())

    price_tag = card.select_one(".a-card__price") or card.select_one(".price")
    price = _extract_price(price_tag.get_text(" ", strip=True) if price_tag else "")

    address_tag = card.select_one(".a-card__subtitle") or card.select_one(".a-card__text-preview")
    address_text = " ".join(address_tag.get_text(" ", strip=True).split()) if address_tag else ""

    district = ""
    if address_text:
        district = address_text.split(",", 1)[0].strip()

    time_tag = card.select_one(".a-card__text-date") or card.select_one(".a-card__header-left")
    published_at = " ".join(time_tag.get_text(" ", strip=True).split()) if time_tag else ""

    photo_urls = _extract_photo_urls(card)
    photo_url = photo_urls[0] if photo_urls else None

    rooms = _extract_rooms(title)

    if not (listing_id and listing_url and title and price is not None):
        return None

    return Listing(
        id=listing_id,
        title=title,
        price=price,
        address=address_text,
        district=district,
        rooms=rooms,
        photo_url=photo_url,
        url=listing_url,
        published_at=published_at,
        photo_urls=photo_urls,
    )


def _build_search_url(
    config: "Config",
    deal_type: str,
    price_min: int | None,
    price_max: int | None,
    area_min: int | None,
    area_max: int | None,
    district_id: int | None = None,
    owner_only: bool | None = None,
    property_type: str | None = None,
) -> str:
    normalized = _normalize_deal_type(deal_type)
    if normalized == "buy":
        path = BUY_LISTINGS_PATH
        default_price_min = 10_000_000
    else:
        path = RENT_LISTINGS_PATH
        default_price_min = None

    base = f"{BASE_URL}{path.format(city=config.city)}"

    params: dict = {"das[_sys.hasphoto]": 1}
    if price_min is not None:
        params["das[price][from]"] = price_min
    elif default_price_min is not None:
        params["das[price][from]"] = default_price_min

    if price_max is not None and price_max > 0:
        params["das[price][to]"] = price_max
    elif price_max is None:
        params["das[price][to]"] = config.max_price

    if area_min is not None:
        params["das[live.square][from]"] = area_min
    if area_max is not None and area_max > 0:
        params["das[live.square][to]"] = area_max

    if config.min_rooms == config.max_rooms:
        params["das[live.rooms]"] = config.max_rooms

    if district_id is not None:
        params["das[district][0]"] = district_id

    # Owner-only filter: das[who]=1 means "from owner only"
    if owner_only:
        params["das[who]"] = 1

    # Property type: das[building.type][]=2 new, das[building.type][]=1 secondary
    if property_type == "new":
        params["das[building.type][]"] = 2
    elif property_type == "secondary":
        params["das[building.type][]"] = 1

    return f"{base}?{urlencode(params)}"


async def parse_krisha(
    config: "Config",
    limit: int | None = None,
    deal_type: str | None = None,
    price_min: int | None = None,
    price_max: int | None = None,
    area_min: int | None = None,
    area_max: int | None = None,
    district: str | None = None,
    owner_only: bool | None = None,
    property_type: str | None = None,
    db: "BotDB | None" = None,
) -> list[Listing]:
    """
    Fetch and parse listings from krisha.kz.

    Args:
        config:        Bot configuration (city, deal_type, max_price, etc.)
        limit:         Optional max number of listings to return.
        deal_type:     Override config.deal_type.
        price_min:     Min price filter.
        price_max:     Max price filter.
        area_min:      Min area filter (m²).
        area_max:      Max area filter (m²).
        district:      User's preferred district name (resolved to ID via city map).
        owner_only:    If True, add das[who]=1 to filter owner-only listings.
        property_type: 'new' or 'secondary' — adds das[building.type][] to URL.
        db:            BotDB instance for error logging (optional).
    """
    await asyncio.sleep(random.uniform(1.0, 3.0))
    resolved_deal_type = deal_type or config.deal_type

    district_id = _resolve_district_id(config.city, district)
    url = _build_search_url(
        config, resolved_deal_type, price_min, price_max, area_min, area_max,
        district_id, owner_only=owner_only, property_type=property_type,
    )

    async with httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=30.0, follow_redirects=True) as client:
        for attempt in (1, 2):
            try:
                response = await client.get(url)
                if response.status_code in (403, 429):
                    msg = f"Krisha blocked with HTTP {response.status_code}"
                    logger.warning(msg)
                    if db:
                        await db.log_parse_error("HTTPBlocked", msg, url)
                    return []
                response.raise_for_status()
                if not _validate_response_scope(str(response.url), config.city, resolved_deal_type):
                    msg = f"Response URL mismatch: city={config.city} deal={resolved_deal_type} got={response.url}"
                    logger.warning(msg)
                    if db:
                        await db.log_parse_error("URLMismatch", msg, url)
                    return []
                break
            except httpx.HTTPStatusError as exc:
                if attempt == 2:
                    logger.exception("HTTP error while loading krisha page")
                    if db:
                        await db.log_parse_error("HTTPStatusError", str(exc), url)
                    return []
                await asyncio.sleep(5)
            except httpx.HTTPError as exc:
                if attempt == 2:
                    logger.exception("Network error while loading krisha page")
                    if db:
                        await db.log_parse_error("NetworkError", str(exc), url)
                    return []
                await asyncio.sleep(5)

    soup = BeautifulSoup(response.text, "html.parser")
    cards = soup.select("div.a-card")
    if not cards:
        cards = soup.select("section.a-card")

    listings: list[Listing] = []
    for card in cards:
        try:
            parsed = _parse_card(card)
        except Exception:
            logger.exception("Error parsing card")
            if db:
                await db.log_parse_error("CardParseError", traceback.format_exc()[:500], url)
            continue

        if not parsed:
            continue

        try:
            if parsed.rooms is not None and not (config.min_rooms <= parsed.rooms <= config.max_rooms):
                continue
            if price_max is not None and price_max > 0 and parsed.price > price_max:
                continue
            if price_max is None and parsed.price > config.max_price:
                continue
            if price_min is not None and parsed.price < price_min:
                continue
        except Exception as exc:
            logger.exception("Error filtering listing id=%s", getattr(parsed, "id", "?"))
            if db:
                await db.log_parse_error("FilterError", str(exc), url)
            continue

        listings.append(parsed)
        if limit and len(listings) >= limit:
            break

    return listings


# ── Investment property support ───────────────────────────────────────────────

STORAGE_PATH = "/prodazha/kommercheskaya-nedvizhimost/{city}/"
GARAGE_PATH  = "/prodazha/garazhi/{city}/"

# Примерные ставки аренды ₸/м²/мес по Астане (для расчёта yield)
# Ставки аренды для кладовок — за м²/мес
STORAGE_RENT_PER_M2: dict[str, int] = {
    "есиль": 4_500,
    "алматы": 4_000,
    "сарыарка": 3_500,
    "нура": 3_000,
    "default": 4_000,
}

# Ставки аренды для паркинга — ПОШТУЧНО за место/мес
PARKING_RENT_FLAT: dict[str, int] = {
    "есиль": 30_000,
    "алматы": 25_000,
    "сарыарка": 20_000,
    "нура": 15_000,
    "default": 25_000,
}

# Ставки для гаражей — ПОШТУЧНО/мес (больше чем паркинг)
GARAGE_RENT_FLAT: dict[str, int] = {
    "есиль": 45_000,
    "алматы": 40_000,
    "сарыарка": 35_000,
    "нура": 30_000,
    "default": 35_000,
}

MIN_YIELD_ALERT = 0.12


def _detect_property_type(title: str, url: str, area: float | None = None) -> str:
    t = title.lower()
    u = url.lower()
    if "кладовк" in t or "кладовая" in t or "storage" in t:
        return "storage"
    if "паркинг" in t or "машиноместо" in t or "парковочн" in t:
        return "parking"
    if "гараж" in t:
        return "garage"
    if "garazhi" in u:
        if area and area >= 25:
            return "garage"
        return "parking"
    return "commercial"


def _detect_district_key(address: str, district: str) -> str:
    text = f"{address} {district}".lower()
    if "есиль" in text or "expo" in text:
        return "есиль"
    if "алмат" in text:
        return "алматы"
    if "сарыарка" in text:
        return "сарыарка"
    if "нура" in text:
        return "нура"
    return "default"


def calc_yield(price: int, area: float | None, prop_type: str,
               address: str = "", district: str = "") -> float | None:
    """
    Считает годовую доходность.
    Паркинг/гараж — поштучная ставка по району.
    Кладовка — за м² по району.
    """
    if price <= 0:
        return None

    dk = _detect_district_key(address, district)

    if prop_type == "parking":
        monthly = PARKING_RENT_FLAT.get(dk, 25_000)
    elif prop_type == "garage":
        monthly = GARAGE_RENT_FLAT.get(dk, 35_000)
    elif prop_type == "storage":
        eff_area = area if (area and area > 0) else 5.0
        rate = STORAGE_RENT_PER_M2.get(dk, 4_000)
        monthly = eff_area * rate
    else:  # commercial
        eff_area = area if (area and area > 0) else 10.0
        monthly = eff_area * 5_000

    return (monthly * 12) / price


async def parse_investment(
    config: "Config",
    limit: int | None = 100,
    max_price: int = 10_000_000,
    max_pages: int = 5,
    db: "BotDB | None" = None,
) -> list[dict]:
    """
    Парсит кладовки, коммерцию и гаражи с продажи.
    Поддерживает пагинацию (до max_pages страниц).
    """
    results = []

    for path in (STORAGE_PATH, GARAGE_PATH):
        base = f"{BASE_URL}{path.format(city=config.city)}"

        for page in range(1, max_pages + 1):
            params = {
                "das[_sys.hasphoto]": 1,
                "das[price][to]": max_price,
            }
            if page > 1:
                params["page"] = page
            url = f"{base}?{urlencode(params)}"

            await asyncio.sleep(random.uniform(2.0, 5.0))

            async with httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=30.0, follow_redirects=True) as client:
                try:
                    resp = await client.get(url)
                    resp.raise_for_status()
                except Exception as exc:
                    logger.warning("parse_investment failed for %s: %s", url, exc)
                    if db:
                        await db.log_parse_error("InvestmentFetchError", str(exc), url)
                    continue

            soup = BeautifulSoup(resp.text, "html.parser")
            cards = soup.select("div.a-card") or soup.select("section.a-card")

            if not cards:
                break  # no more pages

            for card in cards:
                try:
                    listing = _parse_card(card)
                except Exception:
                    continue
                if not listing:
                    continue

                area = _extract_area_from_title(listing.title)
                prop_type = _detect_property_type(listing.title, listing.url, area)
                y = calc_yield(listing.price, area, prop_type,
                               address=listing.address, district=listing.district)

                if y is None or y < MIN_YIELD_ALERT:
                    continue
                if listing.price < 500_000:
                    continue
                if y > 1.0:
                    continue

                d = listing.to_dict()
                d["prop_type"] = prop_type
                d["area"] = area
                d["yield_pct"] = round(y * 100, 1)
                d["payback_years"] = round(1 / y, 1) if y > 0 else None
                dk = _detect_district_key(listing.address, listing.district)
                if prop_type == "parking":
                    d["est_rent"] = PARKING_RENT_FLAT.get(dk, 25_000)
                elif prop_type == "garage":
                    d["est_rent"] = GARAGE_RENT_FLAT.get(dk, 35_000)
                elif prop_type == "storage":
                    d["est_rent"] = int((area or 5.0) * STORAGE_RENT_PER_M2.get(dk, 4_000))
                else:
                    d["est_rent"] = int((area or 10.0) * 5_000)
                d["deal_type"] = "buy"
                results.append(d)

                if limit and len(results) >= limit:
                    break

            if limit and len(results) >= limit:
                break  # stop pagination for this section

    results.sort(key=lambda x: x["yield_pct"], reverse=True)
    return results


def _extract_area_from_title(title: str) -> float | None:
    """Извлекает площадь из заголовка объявления."""
    import re as _re
    m = _re.search(r"(\d+(?:[.,]\d+)?)\s*м", title)
    if m:
        return float(m.group(1).replace(",", "."))
    return None


async def fetch_listing_description(url: str) -> dict[str, str]:
    """
    Fetch individual listing page and extract description + details.
    Returns dict with 'description', 'details' text.
    """
    await asyncio.sleep(random.uniform(3.0, 7.0))

    async with httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=30.0, follow_redirects=True) as client:
        try:
            resp = await client.get(url)
            if resp.status_code in (403, 429):
                logger.warning("Blocked fetching listing %s: HTTP %s", url, resp.status_code)
                return {}
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("Failed to fetch listing %s: %s", url, exc)
            return {}

    soup = BeautifulSoup(resp.text, "html.parser")
    result = {}

    # Description text
    desc_tag = (
        soup.select_one("div.offer__description div.text")
        or soup.select_one("div.offer__description")
        or soup.select_one("[data-name='Description']")
        or soup.select_one(".description")
    )
    if desc_tag:
        result["description"] = " ".join(desc_tag.get_text(" ", strip=True).split())

    # All details/params text (отапливаемый, охрана, год постройки etc)
    details_parts = []
    for tag in soup.select("div.offer__parameters dt, div.offer__parameters dd"):
        details_parts.append(tag.get_text(" ", strip=True))
    for tag in soup.select("div.offer__info-item"):
        details_parts.append(tag.get_text(" ", strip=True))
    for tag in soup.select(".offer__advert-short-info"):
        details_parts.append(tag.get_text(" ", strip=True))

    if details_parts:
        result["details"] = " ".join(details_parts)

    # Try to find year
    year_tag = soup.find(string=re.compile(r"(год\s*(постройки|сдачи)|построен)"))
    if year_tag:
        result["details"] = (result.get("details", "") + " " + year_tag.parent.get_text(" ", strip=True)).strip()

    return result
