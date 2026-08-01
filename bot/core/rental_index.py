"""
Rental index builder.

Parses rental listings from krisha.kz to build median rent
by district and room count. Used for accurate yield calculation.
"""
import asyncio
import logging
import random
import re
from collections import defaultdict
from statistics import median
from urllib.parse import urlencode

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE_URL = "https://krisha.kz"
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9",
}


async def build_rental_index(city="astana", max_pages=3):
    """
    Parse rental listings and return median rent by district and rooms.
    Returns: {
        ("есильский р-н", 1): 180000,
        ("есильский р-н", 2): 250000,
        ("алматы р-н", 1): 150000,
        ...
    }
    """
    rents = defaultdict(list)  # (district_norm, rooms) -> [prices]

    for page in range(1, max_pages + 1):
        params = {"das[_sys.hasphoto]": 1}
        if page > 1:
            params["page"] = page
        url = f"{BASE_URL}/arenda/kvartiry/{city}/?{urlencode(params)}"

        await asyncio.sleep(random.uniform(2.0, 5.0))

        async with httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=30.0, follow_redirects=True) as client:
            try:
                resp = await client.get(url)
                resp.raise_for_status()
            except Exception as exc:
                logger.warning("rental_index: failed page %d: %s", page, exc)
                continue

        soup = BeautifulSoup(resp.text, "html.parser")
        cards = soup.select("div.a-card") or soup.select("section.a-card")

        if not cards:
            break

        for card in cards:
            try:
                # Price
                price_tag = card.select_one(".a-card__price") or card.select_one(".price")
                if not price_tag:
                    continue
                digits = re.sub(r"[^\d]", "", price_tag.get_text())
                if not digits:
                    continue
                price = int(digits)
                if price < 30000 or price > 2000000:
                    continue  # filter noise

                # Title -> rooms
                link_tag = card.select_one("a.a-card__title")
                title = link_tag.get_text(" ", strip=True) if link_tag else ""
                rooms_match = re.search(r"(\d+)\s*-?\s*ком", title.lower())
                rooms = int(rooms_match.group(1)) if rooms_match else None
                if rooms is None or rooms > 5:
                    continue

                # District
                addr_tag = card.select_one(".a-card__subtitle") or card.select_one(".a-card__text-preview")
                addr = addr_tag.get_text(" ", strip=True).lower() if addr_tag else ""
                district = _norm_district(addr)
                if not district:
                    continue

                rents[(district, rooms)].append(price)
            except Exception:
                continue

    # Calculate medians
    index = {}
    for key, prices in rents.items():
        if len(prices) >= 2:
            index[key] = int(median(prices))
        elif len(prices) == 1:
            index[key] = prices[0]

    # Also build district-level median (all rooms)
    district_all = defaultdict(list)
    for (d, r), prices in rents.items():
        district_all[d].extend(prices)
    for d, prices in district_all.items():
        index[(d, 0)] = int(median(prices))  # 0 = any rooms

    logger.info("rental_index: built %d entries from %d raw prices", len(index), sum(len(v) for v in rents.values()))
    return index


def _norm_district(addr):
    """Normalize district name from address."""
    addr = addr.lower().strip()
    for key in ["есильский", "есиль"]:
        if key in addr:
            return "есиль"
    for key in ["алматинский", "алматы р-н", "алматы р"]:
        if key in addr:
            return "алматы"
    if "сарыарка" in addr or "сарыаркинский" in addr:
        return "сарыарка"
    if "нура" in addr:
        return "нура"
    if "байконур" in addr:
        return "байконур"
    return ""


def get_rent_estimate(index, district, rooms):
    """Get rent estimate from index. Falls back to district avg, then city avg."""
    d = _norm_district(district)
    # Exact match
    if (d, rooms) in index:
        return index[(d, rooms)]
    # District average
    if (d, 0) in index:
        return index[(d, 0)]
    # City average
    all_vals = [v for k, v in index.items() if k[1] == rooms]
    if all_vals:
        return int(median(all_vals))
    all_vals = [v for v in index.values()]
    return int(median(all_vals)) if all_vals else None
