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
# Счётчик реальных HTTP-запросов к Крыше за текущий цикл — приблизительная
# оценка нагрузки на источник (см. /admin/api/parser-cycle-history).
# Сбрасывается в service_apartments.run_cycle() перед каждым проходом.
REQUEST_COUNTS = {'search': 0}
# Статистика эффективности detail-fetch за текущий цикл (см. оптимизацию
# ниже в analyze_apartments — пропуск detail-fetch для объявлений без
# изменений). Накапливается за ВСЕ вызовы analyze_apartments() в одном
# цикле (свежий парс + глубокий обход), сбрасывается в
# service_apartments._run_cycle_timed() перед каждым проходом.
# Снимок пишется в parser_cycle_history (total_seen/needs_detail_fetch/
# skipped_no_change) — см. /admin/parsers?tab=recheck, секция "Нагрузка на Крышу".
DETAIL_FETCH_STATS = {'total_seen': 0, 'needs_fetch': 0, 'skipped': 0}

_re_total = _re_mod.compile(r"Найдено[^\d]{0,20}([\d\s\xa0\u2009]{1,12})")
_re_total_clean = _re_mod.compile(r"\D")
from bs4 import BeautifulSoup

from bot.core.rental_parser import lookup_rental_estimate
from bot.core.bargain import get_comparables, analyze_bargain


def _prelim_rank(s: dict, avg_m2: float | None) -> float:
    """Дешёвая предварительная прикидка — используется ТОЛЬКО чтобы выбрать,
    какие объявления в первую очередь отправить на дорогой detail-fetch
    (координаты/ЖК/фото с krisha.kz, ограничен по времени). Настоящий скор
    (Deal Score v3, bot/core/deal_score.py) считается позже по всей базе
    сразу, как только у объявления появятся координаты."""
    price, area = s.get("price") or 0, s.get("area") or 0
    if not price or not area or not avg_m2:
        return 0.0
    price_m2 = price / area
    return max(0.0, (avg_m2 - price_m2) / avg_m2)


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
                                     start_page=1, stats: dict | None = None):
    """Parse apartment sale listings from krisha.kz.
    start_page: с какой страницы начинать (для глубокого обхода всей выдачи).
    max_price: 0/None — БЕЗ потолка цены (нужно для full_sweep.py — иначе
    объявления 100-200М+ ₸ никогда не попадают даже в исходную выдачу с
    Крыши, см. ниже про das[price][to]).
    stats: если передан dict, заполняется pages_ok/pages_failed/reached_end —
    нужно full_sweep.py, чтобы знать, когда реально дошли до конца выдачи,
    а не просто наткнулись на временный сетевой сбой одной страницы."""
    listings = []
    if stats is not None:
        stats.setdefault("pages_ok", 0)
        stats.setdefault("pages_failed", 0)
        stats.setdefault("reached_end", False)

    for page in range(start_page, start_page + max_pages):
        # das[price][to]=0 в реальном запросе к Крыше означает "цена до 0" —
        # т.е. пустая выдача, а не "без ограничения". Раньше max_price всегда
        # был 80 000 000 по умолчанию И НИКОГДА не передавался вызывающими
        # (analyze_apartments его не пробрасывал вообще) — поэтому ВЕСЬ обход
        # (и обычный сервис, и full_sweep) молча ограничивался потолком 80М,
        # и объявления 100-200М+ никогда не попадали даже в скачанную выдачу.
        params = {"das[_sys.hasphoto]": 1}
        if max_price:
            params["das[price][to]"] = max_price
        if page > 1:
            params["page"] = page
        url = f"{BASE_URL}/prodazha/kvartiry/{city}/?{urlencode(params)}"

        await asyncio.sleep(random.uniform(2.0, 5.0))
        async with httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=30.0, follow_redirects=True) as client:
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                REQUEST_COUNTS['search'] += 1
            except Exception as exc:
                logger.warning("apt_parser: page %d failed: %s", page, exc)
                if stats is not None:
                    stats["pages_failed"] += 1
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
            if stats is not None:
                stats["reached_end"] = True
            break
        if stats is not None:
            stats["pages_ok"] += 1

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
                # Скоринг доверия (задача 2026-08-13, "скоринг доверия для
                # каждого объявления") — пока один параметр, тип продавца:
                # "Крыша Агент" — структурный бейдж карточки (label-user-agent,
                # тот же класс, что у фильтра "От Крыша Агентов" —
                # das[_sys.fromAgent] в поиске), НАДЁЖНЕЕ текстового regex
                # (хозяин/владелец не всегда есть в тексте карточки, а бейдж
                # рисуется Крышей детерминированно). Крыша Агент — верифи-
                # цированный Крышей партнёр (её собственная агентская
                # программа), выше доверия, чем рядовой риелтор без бейджа.
                is_krisha_agent = card.select_one(".label-user-agent") is not None
                if is_krisha_agent:
                    seller_type, trust_score = "krisha_agent", 1.0
                elif is_owner:
                    seller_type, trust_score = "owner", 0.8
                else:
                    seller_type, trust_score = "realtor", 0.6
                district = address.split(",")[0].strip() if address else ""

                rooms = _extract_rooms(title)
                area = _extract_area(title)

                time_tag = card.select_one(".a-card__text-date")
                published = time_tag.get_text(strip=True) if time_tag else ""

                # Превью-фото карточки — уже лежит в уже скачанной странице
                # выдачи (das[_sys.hasphoto]=1 гарантирует, что оно есть),
                # берём бесплатно вместо ожидания дорогого запроса детальной
                # страницы (см. coord_backfill.py) отдельно по расписанию.
                # img.a-image__img отдаёт готовый src (не лениво подгружаемый
                # data-src — loading="lazy" тут чисто нативный HTML-атрибут).
                photo_tag = card.select_one("img.a-image__img")
                photo_src = (photo_tag.get("src") or photo_tag.get("data-src")
                             if photo_tag else None)
                photo_url = None
                if photo_src:
                    photo_url = photo_src if photo_src.startswith("http") else f"{BASE_URL}{photo_src}"

                listings.append({
                    "id": lid, "url": listing_url, "title": title,
                    "price": price, "address": address, "district": district,
                    "is_owner": is_owner, "seller_type": seller_type, "trust_score": trust_score,
                    "rooms": rooms, "area": area, "published_at": published,
                    "description": "", "photo_url": photo_url,
                })
            except Exception:
                continue

    logger.info("apt_parser: got %d listings from %d pages", len(listings), max_pages)
    return listings


async def analyze_apartments(city="astana", max_pages=5, start_page=1,
                              max_price=80_000_000, stats: dict | None = None):
    """Full pipeline: parse sales + rentals, score, return sorted results.
    max_price/stats пробрасываются в parse_apartments_for_sale — раньше
    ЭТА функция их не принимала вовсе, поэтому даже явный вызов с
    max_price=0 (см. full_sweep.py) падал с TypeError, а обычный сервисный
    цикл всегда получал скрытый потолок 80М (дефолт parse_apartments_for_sale),
    так что дорогие объявления 100-200М+ не долетали даже до скрапинга."""
    from collections import defaultdict

    # 1. Build rental index
    rental_idx = None  # теперь используем rental_index из PostgreSQL

    # 2. Parse sales
    sales = await parse_apartments_for_sale(city, max_pages=max_pages, start_page=start_page,
                                             max_price=max_price, stats=stats)

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

        # Этаж почти всегда виден прямо в заголовке карточки ("8/9 этаж"
        # или просто "9 этаж") — парсим его тут, не дожидаясь медленного
        # fetch_apartment_details (тот идёт лишь для DETAIL_FETCH_BATCH
        # объявлений за проход и раньше был единственным источником floor).
        if s.get("floor") is None:
            title_text = s.get("title", "") or ""
            fm = re.search(r"(\d+)\s*/\s*(\d+)\s*эт", title_text)
            if fm:
                s["floor"] = int(fm.group(1))
                s["floors_total"] = int(fm.group(2))
            else:
                fm2 = re.search(r"(\d+)\s*эт", title_text)
                if fm2:
                    s["floor"] = int(fm2.group(1))

        # Lookup реальной аренды из PostgreSQL rental_index
        complex_name = s.get("complex_name") or s.get("address", "").split(",")[0].strip()
        rent_data = await lookup_rental_estimate(
            city=city,
            district=d or None,
            complex_name=complex_name or None,
            rooms=rooms,
            prop_type="apartment",
        )
        # Предпочитаем ₸/м² × площадь этой конкретной квартиры вместо голого
        # median_price группы — на широких фолбэках (район/город) комнатность
        # плохо описывает площадь (4-комнатная 70м² и особняк 220м² с той же
        # комнатностью в одной группе), из-за чего плоский median_price мог
        # давать в разы завышенную/заниженную оценку аренды для конкретной
        # площади. На уровне ЖК median_price и так обычно точен, но ₸/м² не
        # хуже и точнее учитывает площадь именно этой квартиры.
        area = s.get("area")
        if rent_data and rent_data.get("price_per_sqm") and area and area > 0:
            rent = round(rent_data["price_per_sqm"] * area)
        else:
            rent = rent_data["median_price"] if rent_data else None
        if rent and s["price"] > 0:
            annual_rent = rent * 12
            s["yield_pct"] = round((annual_rent / s["price"]) * 100, 1)
            # Net-доходность (см. Notion "Расчет доходности"): gross сам по
            # себе занижает картину для карты, но переоценивает её для
            # инвестора — не учитывает простой между жильцами, налог+мелкий
            # ремонт и расходы на саму покупку. Те же допущения, что в
            # методичке: 0.95 — вакантность (~2-3 недели простоя/год при
            # высоком спросе в Астане), 0.10 — налог+косметика (~10% годовой
            # аренды), 1.02 — расходы на покупку (нотариус/риелтор ~2%).
            # net ≈ gross * 0.83 — держим формулу явной (не константой),
            # чтобы менять допущения по отдельности было легко.
            net_annual = annual_rent * 0.95 - annual_rent * 0.10
            purchase_cost = s["price"] * 1.02
            s["net_yield_pct"] = round((net_annual / purchase_cost) * 100, 1) if purchase_cost > 0 else 0
            s["est_rent"] = rent
            s["payback_years"] = round(s["price"] / (rent * 12), 1)
            s["rent_source"] = f"{rent_data.get('level','?')}, n={rent_data.get('sample_count',0)}"
        else:
            s["yield_pct"] = 0
            s["net_yield_pct"] = 0
            s["est_rent"] = 0
            s["payback_years"] = None
            s["rent_source"] = "нет данных" 

        cname = s.get("address", "").split(",")[0].strip().lower()
        same_count = complex_counter.get(cname, 1)
        avg_m2 = district_avg_m2.get(d)

        # Аналоги для анализа торга. На этом шаге координат ещё нет (их
        # даёт fetch_apartment_details ниже) — get_comparables сам падает
        # обратно на район, а Deal Score/аналитика позже пересчитают это
        # уже по гексагону, когда будут известны координаты и ЖК.
        comps, comps_meta = await get_comparables(
            lat=None, lon=None,
            rooms=rooms,
            area=s.get("area"),
            current_price=s.get("price", 0),
            district=d or None,
            exclude_id=s["id"],
        )
        bargain = analyze_bargain(s.get("price", 0), comps, s.get("is_owner"), meta=comps_meta)
        s["bargain_target"] = bargain.get("target_price")
        s["bargain_discount_pct"] = bargain.get("discount_pct")
        s["bargain_rec"] = bargain.get("recommendation")
        s["comparables_cnt"] = bargain.get("comparables_cnt", 0)
        # Задача 2026-08-14 (Фаза A.5 п.2 вердикт-стратегии, подготовка
        # deal_score_snapshots.bargain_method) — meta.method (bargain.py)
        # раньше нигде не сохранялся структурно, только текстом внутри
        # bargain_rec/class_note.
        s["bargain_method"] = comps_meta.get("method")

        s["score_total"] = 0  # реальный скор — позже, в deal_score.apply_deal_scores()
        s["_prelim_rank"] = _prelim_rank(s, avg_m2)
        results.append(s)

    # Fetch detailed info (координаты, фото, отделка) — теперь не только для
    # топа по предварительной прикидке: иначе карта показывает только хорошие
    # объекты (все зелёные), ведь у слабых просто никогда не появляются
    # координаты. Берём половину батча — лучшие по prelim_rank (для точности
    # топа), половину — случайную выборку из остальных (чтобы карта отражала
    # реальный разброс качества).
    results.sort(key=lambda x: x["_prelim_rank"], reverse=True)

    from bot.db import settings as _app_settings
    detail_batch = _app_settings.get_int("DETAIL_FETCH_BATCH", 15)

    # ОПТИМИЗАЦИЯ (см. задачу "оптимизация работы парсеров"): раньше сюда
    # попадала top_half+random_half выборка из ВСЕХ объявлений на странице
    # выдачи каждый цикл — включая те, что мы уже детально обошли вчера и
    # позавчера и у которых с тех пор ничего не изменилось. detail_fetch
    # (координаты/фото/описание) — самая дорогая и медленная операция
    # (8-15с пауза на объявление, отдельный HTTP-запрос на страницу деталей).
    # Цена/ID/адрес мы и так получаем бесплатно одним запросом на страницу
    # выдачи (20-30 объявлений за раз) — этого достаточно, чтобы понять,
    # НУЖНО ли вообще лезть в детали. Правило: лезем только если
    #   1) объявления совсем нет в базе (новое) — нужны координаты/фото, или
    #   2) цена изменилась с прошлого раза — стоит перепроверить и обновить
    #      историю цены, или
    #   3) объявление уже есть, но координат так и не появилось (прошлый
    #      detail-fetch не удался/не был сделан) — досмотреть его ещё раз.
    # Иначе (цена та же, координаты уже есть) — пропускаем, объявление и так
    # видно на карте с прошлого раза. По прикидке из задачи — это должно
    # сократить число detail-запросов в десятки раз на устоявшемся ядре базы.
    known_by_id: dict[str, tuple] = {}
    ids_on_page = [r["id"] for r in results if r.get("id")]
    if ids_on_page:
        try:
            from bot.db.pg import fetch as _pg_fetch
            known_rows = await _pg_fetch(
                "SELECT id, price, lat FROM apartment_listings WHERE id = ANY($1::text[])",
                ids_on_page)
            known_by_id = {row["id"]: (row["price"], row["lat"]) for row in known_rows}
        except Exception as e:
            logger.warning("apt_parser: не удалось прочитать известные цены из БД (%s) — "
                            "работаем как раньше, без фильтра по изменению цены", e)

    def _needs_detail_fetch(r: dict) -> bool:
        if r.get("details_fetched"):
            return False
        known = known_by_id.get(r["id"])
        if known is None:
            return True  # новое объявление — не встречалось в базе вовсе
        known_price, known_lat = known
        if known_price != r.get("price"):
            return True  # цена изменилась с прошлого обхода
        if known_lat is None:
            return True  # уже видели, но детали так и не подъехали — досмотрим ещё раз
        return False  # без изменений, координаты уже есть — пропускаем

    all_candidates = [r for r in results if not r.get("details_fetched")]
    candidates = [r for r in all_candidates if _needs_detail_fetch(r)]
    skipped = len(all_candidates) - len(candidates)
    logger.info("apt_parser: %d объявлений на странице, %d требуют detail-fetch "
                "(новые/цена изменилась/нет координат), %d пропущено без изменений",
                len(all_candidates), len(candidates), skipped)
    DETAIL_FETCH_STATS['total_seen'] += len(all_candidates)
    DETAIL_FETCH_STATS['needs_fetch'] += len(candidates)
    DETAIL_FETCH_STATS['skipped'] += skipped

    half = max(1, detail_batch // 2)
    top_half = candidates[:half]
    rest = candidates[half:]
    random_half = random.sample(rest, min(half, len(rest))) if rest else []
    to_fetch = top_half + random_half

    from bot.core.apartment_details import fetch_apartment_details
    for r in to_fetch:
        url = r.get("url", "")
        if url:
            logger.info("fetching details for %s (prelim_rank=%.2f)", r["id"], r["_prelim_rank"])
            await asyncio.sleep(random.uniform(8.0, 15.0))  # пауза чтобы не блокировали
            details = await fetch_apartment_details(url)
            if details:
                r.update(details)
                r["details_fetched"] = True
                # Настоящий скор посчитается в следующем проходе
                # deal_score.apply_deal_scores() — теперь, когда есть координаты.


    results.sort(key=lambda x: x["_prelim_rank"], reverse=True)
    return results