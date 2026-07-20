"""
Enhanced apartment detail fetcher.
Extracts ALL signals from krisha.kz listing page.
"""
import asyncio, logging, random, re
import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9",
}

# Known developers in Astana
KNOWN_DEVELOPERS = [
    "bi group", "bi-group", "bazis-a", "bazis a", "sensata",
    "m2", "em2", "баспана", "think green", "эталон", "globus",
    "prime", "daniyar", "rams", "абажур", "grand", "asyl",
    "orda", "нурсая", "арт хаус", "art house", # комфорт убран — слишком generic
]

# Renovation levels
RENOVATION_MAP = {
    "дизайнерский": "designer",
    "евроремонт": "euro",
    "чистовая": "finish",
    "чистовой": "finish",
    "черновая": "rough",
    "черновой": "rough",
    "без ремонта": "none",
    "требует ремонта": "needs_repair",
    "хороший ремонт": "good",
    "свежий ремонт": "fresh",
    "косметический": "cosmetic",
}

VIEW_KEYWORDS = {
    "вид на воду": "water",
    "вид на реку": "water",
    "вид на озеро": "water",
    "вид на горы": "mountains",
    "вид на парк": "park",
    "вид на город": "city",
    "панорамный вид": "panoramic",
    "хан шатыр": "landmark",
    "байтерек": "landmark",
    "expo": "expo",
    "экспо": "expo",
}

UTILITY_PATTERNS = [
    r"кк[уy]\s*[—\-~≈]\s*(\d[\d\s,\.]+)\s*(тыс|₸|тг)",
    r"коммунальн\w+\s*[—\-~≈]\s*(\d[\d\s,\.]+)\s*(тыс|₸|тг)",
    r"кск\s*[—\-~≈]\s*(\d[\d\s,\.]+)\s*(тыс|₸|тг)",
    r"оси\s*[—\-~≈]\s*(\d[\d\s,\.]+)\s*(тыс|₸|тг)",
]


async def fetch_apartment_details(url: str) -> dict:
    """
    Fetch full listing details from krisha.kz listing page.
    Returns rich dict with all extractable signals.
    """
    await asyncio.sleep(random.uniform(3.0, 6.0))

    async with httpx.AsyncClient(headers=HEADERS, timeout=30.0, follow_redirects=True) as c:
        try:
            resp = await c.get(url)
            if resp.status_code in (403, 429):
                logger.warning("blocked: %s", url)
                return {}
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("fetch_apartment_details failed %s: %s", url, exc)
            return {}

    soup = BeautifulSoup(resp.text, "html.parser")
    result = {}

    # ── Title & price flags ──────────────────────────────────────────────
    title_el = soup.select_one("h1") or soup.select_one(".offer__title")
    title = title_el.get_text(" ", strip=True) if title_el else ""
    result["title_full"] = title

    price_el = soup.select_one(".offer__price")
    price_text = price_el.get_text(" ", strip=True) if price_el else ""
    result["is_price_from"] = bool(re.search(r"\bот\b", price_text.lower()) or
                                    re.search(r"\bот\b", title.lower()))
    if result["is_price_from"]:
        result["price_warning"] = "⚠️ ЦЕНА 'ОТ' — маркетинговая ловушка, реальная цена выше"

    # ── Structured parameters from offer__info-item ──────────────────────
    params = {}
    for item in soup.select("div.offer__info-item"):
        text = item.get_text(" ", strip=True)
        parts = text.split(None, 1)
        if len(parts) >= 2:
            # Try to match known field names
            full = item.get_text(" ", strip=True)
            params[full.split()[0].lower()] = full
    # Better: iterate dt/dd in info items
    for item in soup.select("div.offer__info-item"):
        children = list(item.children)
        texts = [c.get_text(strip=True) for c in item.children
                 if hasattr(c, "get_text") and c.get_text(strip=True)]
        if len(texts) >= 2:
            key = texts[0].lower().strip()
            val = texts[1].strip()
            params[key] = val

    # Infer from full text of all info items
    all_info = " ".join(i.get_text(" ", strip=True)
                        for i in soup.select("div.offer__info-item"))

    # Floor
    floor_match = re.search(r"(\d+)\s*из\s*(\d+)", all_info)
    if floor_match:
        result["floor"] = int(floor_match.group(1))
        result["floors_total"] = int(floor_match.group(2))
        f, ft = result["floor"], result["floors_total"]
        if f == 1:
            result["floor_position"] = "first"
            result["floor_note"] = "1-й этаж — дисконт ~5-10%"
        elif f == ft:
            result["floor_position"] = "last"
            result["floor_note"] = "последний этаж — дисконт ~3-5%"
        else:
            result["floor_position"] = "middle"
            result["floor_note"] = ""

    # Year built
    year_match = re.search(r"(\b20[0-2]\d\b|\b199\d\b|\b198\d\b)", all_info)
    if year_match:
        result["year_built"] = int(year_match.group(1))
        y = result["year_built"]
        if y >= 2020:
            result["building_age_class"] = "new"
        elif y >= 2010:
            result["building_age_class"] = "modern"
        elif y >= 2000:
            result["building_age_class"] = "old"
        else:
            result["building_age_class"] = "soviet"

    # Building type
    if "кирпич" in all_info.lower():
        result["building_type"] = "кирпичный"
    elif "панел" in all_info.lower():
        result["building_type"] = "панельный"
        result["building_type_note"] = "панельный — хуже теплоизоляция"
    elif "монолит" in all_info.lower():
        result["building_type"] = "монолитный"
    elif "блочн" in all_info.lower():
        result["building_type"] = "блочный"

    # Area details
    area_match = re.search(r"(\d+(?:[.,]\d+)?)\s*м²", all_info)
    if area_match:
        result["area_total"] = float(area_match.group(1).replace(",", "."))
    kitchen_match = re.search(r"кухн\w*\s*[—\-]\s*(\d+(?:[.,]\d+)?)\s*м²", all_info.lower())
    if kitchen_match:
        result["kitchen_area"] = float(kitchen_match.group(1).replace(",", "."))

    # ── DL extra params ──────────────────────────────────────────────────
    for dl in soup.select("dl"):
        dt = dl.select_one("dt")
        dd = dl.select_one("dd")
        if dt and dd:
            k = dt.get_text(strip=True).lower()
            v = dd.get_text(strip=True)
            if "потолок" in k or "высота" in k:
                result["ceiling_height"] = v
            elif "безопасность" in k or "охрана" in k:
                result["security"] = v
            elif "мебел" in k:
                result["furniture_param"] = v
            elif "ремонт" in k or "состояни" in k:
                result["renovation_param"] = v
            elif "балкон" in k or "лоджи" in k:
                result["balcony"] = v
            elif "паркинг" in k or "парковк" in k:
                result["parking"] = v
            elif "интернет" in k:
                result["internet"] = v
            elif "обмен" in k:
                result["has_exchange"] = v

    # ── Full description ──────────────────────────────────────────────────
    desc_el = soup.select_one("div.offer__description")
    desc_text = desc_el.get_text(" ", strip=True) if desc_el else ""
    result["description"] = desc_text[:2000]

    combined = f"{title} {all_info} {desc_text}".lower()

    # ── New build detection ───────────────────────────────────────────────
    new_build_kw = ["новостройка", "от застройщика", "первичн", "новый жк",
                    "строящ", "сдача", "сдан в", "введен в эксплуатаци"]
    result["is_new_build"] = any(kw in combined for kw in new_build_kw)

    # ── Developer detection ───────────────────────────────────────────────
    for dev in KNOWN_DEVELOPERS:
        if dev in combined:
            result["developer_name"] = dev.title()
            result["developer_known"] = True
            break
    if not result.get("developer_name"):
        # Try to find "застройщик: X" pattern
        dev_match = re.search(r"застройщик[:\s]+([А-ЯЁA-Z][^\n,\.]{2,30})", desc_text, re.IGNORECASE)
        if dev_match:
            result["developer_name"] = dev_match.group(1).strip()
            result["developer_known"] = False

    # ── Complex name ─────────────────────────────────────────────────────
    # 1) Официальный блок Крыши «Жилой комплекс» со ссылкой на карточку ЖК
    #    (data-name="map.complex") — это каноническое название ЖК и прямая
    #    ссылка на его страницу (там адрес и карта ЖК). Самый надёжный источник.
    cx_el = soup.select_one(
        'div.offer__info-item[data-name="map.complex"] a[href*="/complex/show/"]')
    if not cx_el:
        # запасной вариант — любая ссылка на карточку ЖК на странице
        cx_el = soup.select_one('a[href*="/complex/show/"]')
    if cx_el:
        cx_name = cx_el.get_text(strip=True)
        if cx_name:
            result["complex_name"] = cx_name
            href = cx_el.get("href", "")
            result["complex_url"] = (
                "https://krisha.kz" + href) if href.startswith("/") else href

    # 2) Фолбэк: поиск названия ЖК в тексте (заголовок/параметры/описание)
    if not result.get("complex_name"):
        complex_patterns = [
            r"жк\s+[«\"']?([А-ЯЁA-Zа-яёa-z][^\n,\.«»\"']{2,30})[«»\"']?",
            r"жилой\s+комплекс\s+[«\"']?([^\n,\.«»\"']{3,30})",
            r"[«\"']([А-ЯЁ][А-ЯЁа-яёa-z\s]{2,25})[»\"']\s*(жк|жилой|резид)",
        ]
        for pat in complex_patterns:
            m = re.search(pat, combined, re.IGNORECASE)
            if m:
                result["complex_name"] = m.group(1).strip().title()
                break

    # ── Seller type ────────────────────────────────────────────────────────
    seller_el = (soup.select_one(".offer__seller-type") or
                 soup.select_one(".owner-label") or
                 soup.select_one("[class*='seller']"))
    if seller_el:
        st = seller_el.get_text(strip=True).lower()
        if "хозяин" in st or "собственник" in st:
            result["seller_type"] = "owner"
            result["seller_note"] = "хозяин — можно торговаться"
        elif "риелтор" in st or "агент" in st:
            result["seller_type"] = "agent"
            result["seller_note"] = "риелтор — есть комиссия"
        elif "застройщик" in st:
            result["seller_type"] = "developer"

    # ── Renovation from text ──────────────────────────────────────────────
    for kw, val in RENOVATION_MAP.items():
        if kw in combined:
            result["renovation"] = val
            result["renovation_text"] = kw
            break

    # ── Furniture ─────────────────────────────────────────────────────────
    if "с мебелью" in combined or "меблированн" in combined:
        result["furniture"] = True
    elif "без мебели" in combined:
        result["furniture"] = False

    # ── View signals ──────────────────────────────────────────────────────
    views = []
    for kw, tag in VIEW_KEYWORDS.items():
        if kw in combined:
            views.append(tag)
    if views:
        result["views"] = views
        result["has_premium_view"] = any(v in views for v in ["water", "panoramic", "landmark", "expo"])

    # ── Utilities / КСК / ОСИ ────────────────────────────────────────────
    for pat in UTILITY_PATTERNS:
        m = re.search(pat, combined, re.IGNORECASE)
        if m:
            result["utilities_amount"] = m.group(1).strip()
            result["utilities_text"] = m.group(0)
            break

    if "кск" in combined:
        result["has_ksk"] = True
    if "оси" in combined:
        result["has_osi"] = True
    if "проблем" in combined and ("кск" in combined or "оси" in combined):
        result["management_problems"] = True
        result["management_note"] = "⚠️ упоминаются проблемы с управлением дома"

    # ── Address detail ────────────────────────────────────────────────────
    addr_el = soup.select_one(".offer__location") or soup.select_one(".offer__map-address")
    if addr_el:
        result["address_full"] = addr_el.get_text(" ", strip=True)

    # ── Publication date ─────────────────────────────────────────────────
    # Date: look for specific patterns, not generic selectors
    import re as _re
    # Try to find date in page text
    date_patterns = [
        r"(\d{1,2}\s+(?:январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр)\w*\s+\d{4})",
        r"(\d{1,2}\.\d{2}\.\d{4})",
        r"сегодня|вчера|\d+\s+дн",
    ]
    page_text = soup.get_text(" ", strip=True)[:3000]
    for pat in date_patterns:
        dm = _re.search(pat, page_text, _re.IGNORECASE)
        if dm:
            result["published_raw"] = dm.group(0)
            break
    # Also look for "с X" pattern like "с 4 июня"
    since_match = _re.search(r"объявление посмотрел\w*\s+с\s+(\d+\s+\w+)", page_text, _re.IGNORECASE)
    if since_match:
        result["published_since"] = since_match.group(1)

    # ── Координаты объявления (для зон приоритета) ────────────────────────
    # ── Фото: og:image + галерея (URL с CDN, сами файлы не качаем) ──
    photo_urls = []
    og = _re.search(r'property="og:image"\s+content="([^"]+)"', resp.text)
    if og:
        photo_urls.append(og.group(1))
    for m in _re.finditer(r'https://[^\s"\'<>]+?(?:photos|photo)[^\s"\'<>]*?\.(?:jpe?g|webp)', resp.text):
        u = m.group(0)
        if u not in photo_urls:
            photo_urls.append(u)
    # Последнее фото галереи — рекламный баннер Крыши, не наше. Выкидываем.
    # (обрезаем ДО лимита в 15, иначе при длинной галерее баннер не попал бы
    # в хвост и мы бы срезали настоящее фото)
    if len(photo_urls) >= 2:
        photo_urls = photo_urls[:-1]
    photo_urls = photo_urls[:15]
    if photo_urls:
        result["photos"] = photo_urls

    # krisha кладёт координаты в JS-блоб: "lat":51.xx ... "lon":71.xx
    lat_m = _re.search(r'"lat"\s*:\s*(\d{2}\.\d+)', resp.text)
    lon_m = _re.search(r'"lon"\s*:\s*(\d{2}\.\d+)', resp.text)
    if lat_m and lon_m:
        try:
            lat, lon = float(lat_m.group(1)), float(lon_m.group(1))
            # sanity: Астана ~ 51.0-51.3, 71.2-71.7
            if 50.0 < lat < 53.0 and 69.0 < lon < 73.0:
                result["lat"], result["lon"] = lat, lon
        except ValueError:
            pass

    # ── Имя продавца/риелтора ────────────────────────────────────────────
    # Крыша по соображениям приватности обычно показывает только ИМЯ (без
    # фамилии) рядом с блоком контактов/телефона. Паттерн best-effort — не
    # на живой разметке, а по типовым JSON-полям объявлений; если не
    # найдётся, просто не заполняем (не критично, есть is_owner).
    for pat in (r'"userName"\s*:\s*"([^"\d][^"]{1,40})"',
               r'"contactName"\s*:\s*"([^"\d][^"]{1,40})"',
               r'"author"\s*:\s*\{\s*"name"\s*:\s*"([^"\d][^"]{1,40})"'):
        m = _re.search(pat, resp.text)
        if m:
            name = m.group(1).strip()
            if name and len(name) <= 40:
                result["seller_name"] = name
            break

    # ── Архивность: объявление снято/продано ─────────────────────────────
    result["is_archived"] = ("В архиве" in resp.text
                             or "может быть неактуальным" in resp.text)

    logger.info("fetch_apartment_details: extracted %d fields from %s",
                len(result), url)
    return result
