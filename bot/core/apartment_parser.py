"""
Apartment investment parser.

Parses sale listings, calculates yield using rental index,
scores and returns top candidates.
"""
import asyncio
import logging
import random
import re
from collections import Counter
from urllib.parse import urlencode

import httpx
import re as _re_mod

LAST_TOTAL_FOUND: int | None = None
_re_total = _re_mod.compile(r"Найдено[^\d]{0,20}([\d\s\xa0\u2009]{1,12})")
_re_total_clean = _re_mod.compile(r"\D")
from bs4 import BeautifulSoup

from bot.core.rental_parser import lookup_rental_estimate
from bot.core.bargain import get_comparables, analyze_bargain

def _norm_district(district: str) -> str:
    """Normalize district name for lookup."""
    d = district.lower().strip()
    if "есил" in d: return "есиль"
    if "алматы" in d or "алматинский" in d: return "алматы"
    if "сарыарка" in d or "сарыаркинский" in d: return "сарыарка"
    if "нура" in d: return "нура"
    if "байконур" in d: return "байконур"
    return d


logger = logging.getLogger(__name__)

BASE_URL = "https://krisha.kz"
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9",
}


def _extract_area(title):
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*м", title)
    return float(m.group(1).replace(",", ".")) if m else None


def _extract_rooms(title):
    m = re.search(r"(\d+)\s*-?\s*ком", title.lower())
    return int(m.group(1)) if m else None


async def parse_apartments_for_sale(city="astana", max_pages=5, max_price=80_000_000,
                                     start_page=1):
    """Parse apartment sale listings from krisha.kz.
    start_page: с какой страницы начинать (для глубокого обхода всей выдачи)."""
    listings = []

    for page in range(start_page, start_page + max_pages):
        params = {"das[_sys.hasphoto]": 1, "das[price][to]": max_price}
        if page > 1:
            params["page"] = page
        url = f"{BASE_URL}/prodazha/kvartiry/{city}/?{urlencode(params)}"

        await asyncio.sleep(random.uniform(2.0, 5.0))
        async with httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=30.0, follow_redirects=True) as client:
            try:
                resp = await client.get(url)
                resp.raise_for_status()
            except Exception as exc:
                logger.warning("apt_parser: page %d failed: %s", page, exc)
                continue

        # Общее число объявлений в выдаче ("Найдено N объявлений") — для
        # детерминированного конца глубокого обхода (последняя страница =
        # ceil(N/20)). Обновляется при каждом парсе первой страницы.
        global LAST_TOTAL_FOUND
        m_total = _re_total.search(resp.text)
        if m_total:
            try:
                LAST_TOTAL_FOUND = int(_re_total_clean.sub("", m_total.group(1)))
            except ValueError:
                pass

        soup = BeautifulSoup(resp.text, "html.parser")
        cards = soup.select("div.a-card") or soup.select("section.a-card")
        if not cards:
            break

        for card in cards:
            try:
                link = card.select_one("a.a-card__title")
                if not link:
                    continue
                href = link.get("href", "")
                listing_url = f"{BASE_URL}{href}" if not href.startswith("http") else href
                title = " ".join(link.get_text(" ", strip=True).split())

                lid_match = re.search(r"/(\d{5,})", href)
                lid = lid_match.group(1) if lid_match else None
                if not lid:
                    continue

                price_tag = card.select_one(".a-card__price") or card.select_one(".price")
                digits = re.sub(r"[^\d]", "", price_tag.get_text() if price_tag else "")
                if not digits:
                    continue
                price = int(digits)
                if price < 5_000_000:
                    continue

                addr_tag = card.select_one(".a-card__subtitle") or card.select_one(".a-card__text-preview")
                address = " ".join(addr_tag.get_text(" ", strip=True).split()) if addr_tag else ""

                # Продавец: бейдж/текст карточки ("Хозяин недвижимости" и т.п.)
                card_text = card.get_text(" ", strip=True)
                is_owner = bool(re.search(r"хозяин|владел|собственник", card_text, re.IGNORECASE))
                district = address.split(",")[0].strip() if address else ""

                rooms = _extract_rooms(title)
                area = _extract_area(title)

                time_tag = card.select_one(".a-card__text-date")
                published = time_tag.get_text(strip=True) if time_tag else ""

                listings.append({
                    "id": lid, "url": listing_url, "title": title,
                    "price": price, "address": address, "district": district,
                    "is_owner": is_owner,
                    "rooms": rooms, "area": area, "published_at": published,
                    "description": "",
                })
            except Exception:
                continue

    logger.info("apt_parser: got %d listings from %d pages", len(listings), max_pages)
    return listings


async def analyze_apartments(city="astana", max_pages=5, start_page=1):
    """Full pipeline: parse sales + rentals, score, return sorted results."""
    from bot.core.apartment_score_v2 import compute_apartment_score_v2 as compute_apartment_score
    from collections import defaultdict

    # 1. Build rental index
    rental_idx = None  # теперь используем rental_index из PostgreSQL

    # 2. Parse sales
    sales = await parse_apartments_for_sale(city, max_pages=max_pages, start_page=start_page)

    # 3. Build price-per-m2 index by district
    district_prices = defaultdict(list)
    for s in sales:
        if s["area"] and s["area"] > 0 and s["price"]:
            d = _norm_district(s.get("district", ""))
            if d:
                district_prices[d].append(s["price"] / s["area"])

    from statistics import median
    district_avg_m2 = {d: median(prices) for d, prices in district_prices.items() if prices}

    # 4. Count per complex
    complex_counter = Counter()
    for s in sales:
        cname = s.get("address", "").split(",")[0].strip().lower()
        complex_counter[cname] += 1

    # 5. Score each listing
    results = []
    for s in sales:
        rooms = s.get("rooms")
        district = s.get("district", "")
        d = _norm_district(district)

        # Lookup реальной аренды из PostgreSQL rental_index
        complex_name = s.get("complex_name") or s.get("address", "").split(",")[0].strip()
        rent_data = await lookup_rental_estimate(
            city=city,
            district=d or None,
            complex_name=complex_name or None,
            rooms=rooms,
            prop_type="apartment",
        )
        rent = rent_data["median_price"] if rent_data else None
        if rent and s["price"] > 0:
            s["yield_pct"] = round((rent * 12 / s["price"]) * 100, 1)
            s["est_rent"] = rent
            s["payback_years"] = round(s["price"] / (rent * 12), 1)
            s["rent_source"] = f"{rent_data.get('level','?')}, n={rent_data.get('sample_count',0)}"
        else:
            s["yield_pct"] = 0
            s["est_rent"] = 0
            s["payback_years"] = None
            s["rent_source"] = "нет данных" 

        cname = s.get("address", "").split(",")[0].strip().lower()
        same_count = complex_counter.get(cname, 1)
        avg_m2 = district_avg_m2.get(d)

        # Аналоги для анализа торга
        comps = await get_comparables(
            district=d or None,
            rooms=rooms,
            area=s.get("area"),
            current_price=s.get("price", 0),
            exclude_id=s["id"],
        )
        bargain = analyze_bargain(s.get("price", 0), comps, s.get("is_owner"))
        s["bargain_target"] = bargain.get("target_price")
        s["bargain_discount_pct"] = bargain.get("discount_pct")
        s["bargain_rec"] = bargain.get("recommendation")
        s["comparables_cnt"] = bargain.get("comparables_cnt", 0)

        score = compute_apartment_score(
            s,
            monthly_rent=rent,
            same_complex_count=same_count,
            district_avg_m2=avg_m2,
            comparables=comps,
        )
        s["score_data"] = score
        s["score_total"] = score["total_score"]
        results.append(s)

    # Fetch detailed info (координаты, фото, отделка) — теперь не только для
    # топ-скора: иначе карта показывает только хорошие объекты (все зелёные),
    # ведь у слабых просто никогда не появляются координаты. Берём половину
    # батча — лучшие по скору (для точности топа), половину — случайную
    # выборку из остальных (чтобы карта отражала реальный разброс качества).
    results.sort(key=lambda x: x["score_total"], reverse=True)

    from bot.db import settings as _app_settings
    detail_batch = _app_settings.get_int("DETAIL_FETCH_BATCH", 15)

    candidates = [r for r in results if not r.get("details_fetched")]
    half = max(1, detail_batch // 2)
    top_half = candidates[:half]
    rest = candidates[half:]
    random_half = random.sample(rest, min(half, len(rest))) if rest else []
    to_fetch = top_half + random_half

    from bot.core.apartment_details import fetch_apartment_details
    for r in to_fetch:
        url = r.get("url", "")
        if url:
            logger.info("fetching details for %s (score=%d)", r["id"], r["score_total"])
            await asyncio.sleep(random.uniform(8.0, 15.0))  # пауза чтобы не блокировали
            details = await fetch_apartment_details(url)
            if details:
                r.update(details)
                r["details_fetched"] = True
                # Re-score with new info
                cname = r.get("address", "").split(",")[0].strip().lower()
                same_count = complex_counter.get(cname, 1)
                d = _norm_district(r.get("district", ""))
                avg_m2 = district_avg_m2.get(d)
                score = compute_apartment_score(r, monthly_rent=r.get("est_rent"), same_complex_count=same_count, district_avg_m2=avg_m2)
                r["score_data"] = score
                r["score_total"] = score["total_score"]


    results.sort(key=lambda x: x["score_total"], reverse=True)
    return results