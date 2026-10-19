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
from datetime import datetime, timezone
import time
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode, urlparse, parse_qs

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


def _sale_page_evidence(soup, page):
    """A missing selector is not an empty market; challenges never mean EOF."""
    text = soup.get_text(" ", strip=True).lower()
    main = soup.select_one(".a-list.a-search-list")
    cards = (main.select(":scope > div.a-card, :scope > section.a-card") if main is not None
             else soup.select("div.a-card, section.a-card"))
    if re.search(r"подтвердите.{0,30}(робот|человек)|access denied|just a moment", text) or (
            not cards and re.search(r"captcha|капч|провер.{0,20}(робот|человек)", text)):
        raise ValueError("source_challenge")
    count = _re_total.search(soup.get_text(" ", strip=True))
    total = int(_re_total_clean.sub("", count.group(1))) if count else None
    if not cards:
        empty = (soup.select_one(".a-list-empty") is not None or re.search(
            r"ничего не найдено|объявлени[йя] не найдено|найдено\s+0\s+объявлен", text))
        if not empty:
            raise ValueError("unrecognised_search_html")
        if page == 1 and total != 0:
            raise ValueError("unconfirmed_empty_head")
        return [], {"ids": [], "total_count": total,
                    "terminal_reason": "explicit_empty", "expected_last_page": None}
    ids = []
    for card in cards:
        link = card.select_one('a[href*="/a/show/"]')
        match = re.search(r"/a/show/(\d{5,})", link.get("href", "")) if link else None
        identifier = str(card.get("data-id", "")).strip() or (match.group(1) if match else "")
        if not re.fullmatch(r"\d{5,}", identifier) or (match and match.group(1) != identifier):
            raise ValueError("invalid_or_conflicting_card_id")
        ids.append(identifier)
    links = [link for box in soup.select(".paginator, .pagination, [data-pagination]")
             for link in box.select('a[href]')]
    links += soup.select('a[rel="last"][href]')
    numbers = [int(match.group(1)) for link in links
               if (match := re.search(r"[?&]page=(\d+)", link.get("href", "")))]
    has_later = any(n > page for n in numbers)
    explicit_last = any("last" in (link.get("rel") or []) and
                        re.search(rf"[?&]page={page}(?:&|$)", link.get("href", "")) for link in links)
    count_last = (total + 19) // 20 if total else None
    # A current parsed counter plus real source cards is evidence, unlike
    # the service's old planned snapshot. Pagination that says more wins.
    terminal = not has_later and (explicit_last or (count_last == page))
    return cards, {"ids": ids, "total_count": total,
                   "expected_last_page": max([count_last or 0, *numbers]) or None,
                   "terminal_reason": ("explicit_last_page" if explicit_last else "observed_catalog_count") if terminal else None}


async def parse_apartments_for_sale(city="astana", max_pages=5, max_price=None,
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
        stats.setdefault("page_reports", [])
        stats.setdefault("page_elapsed_sec", 0.0)

    for page in range(start_page, start_page + max_pages):
        # das[price][to]=0 в реальном запросе к Крыше означает "цена до 0" —
        # т.е. пустая выдача, а не "без ограничения". Раньше max_price всегда
        # был 80 000 000 по умолчанию И НИКОГДА не передавался вызывающими
        # (analyze_apartments его не пробрасывал вообще) — поэтому ВЕСЬ обход
        # (и обычный сервис, и full_sweep) молча ограничивался потолком 80М,
        # и объявления 100-200М+ никогда не попадали даже в скачанную выдачу.
        # Collect the entire price range, including listings without photos.
        # An explicit max_price is still supported for deliberate narrow scans.
        params = {}
        if max_price:
            params["das[price][to]"] = max_price
        if page > 1:
            params["page"] = page
        url = f"{BASE_URL}/prodazha/kvartiry/{city}/?{urlencode(params)}"

        started = time.monotonic()
        report = {"page": page, "ids": [], "accepted_ids": [], "rejected": {},
                  "checked_at": datetime.now(timezone.utc).isoformat()}
        await asyncio.sleep(random.uniform(2.0, 5.0))
        async with httpx.AsyncClient(headers=DEFAULT_HEADERS, timeout=30.0, follow_redirects=True) as client:
            try:
                REQUEST_COUNTS['search'] += 1
                resp = await client.get(url)
                resp.raise_for_status()
                report["checked_at"] = datetime.now(timezone.utc).isoformat()
                final = urlparse(str(resp.url))
                expected = urlparse(url)
                if (final.hostname not in ("krisha.kz", "www.krisha.kz")
                        or final.path.rstrip("/") != expected.path.rstrip("/")
                        or parse_qs(final.query).get("page", ["1"]) != [str(page)]):
                    raise ValueError("redirected_search_scope_or_page")
                soup = BeautifulSoup(resp.text, "html.parser")
                cards, evidence = _sale_page_evidence(soup, page)
                report.update(evidence)
                if cards and not report.get("terminal_reason") and stats is not None and any(
                        previous.get("ids") and set(previous["ids"]) == set(report["ids"])
                        for previous in stats["page_reports"]):
                    raise ValueError("repeated_page_without_terminal_proof")
            except Exception as exc:
                report["checked_at"] = datetime.now(timezone.utc).isoformat()
                logger.warning("apt_parser: page %d failed: %s", page, exc)
                status = getattr(getattr(exc, "response", None), "status_code", None)
                report.update(error=f"http_{status}" if status else str(exc)[:120],
                              blocked=status in (403, 429, 468) or str(exc) == "source_challenge")
                retry = getattr(getattr(exc, "response", None), "headers", {}).get("Retry-After", "")
                try:
                    report["retry_after_seconds"] = max(0, int(retry))
                except ValueError:
                    try:
                        report["retry_after_seconds"] = max(0, (parsedate_to_datetime(retry) - datetime.now(timezone.utc)).total_seconds())
                    except (ValueError, TypeError, OverflowError):
                        pass
                if stats is not None:
                    stats["pages_failed"] += 1
                    stats["blocked"] = report["blocked"]
                    stats["page_reports"].append(report)
                    stats["page_elapsed_sec"] += time.monotonic() - started
                # Do not spray the remaining batch at a failing source and
                # never let a later success skip this page.
                break

        # Общее число объявлений в выдаче ("Найдено N объявлений") — для
        # детерминированного конца глубокого обхода (последняя страница =
        # ceil(N/20)). Обновляется при каждом парсе первой страницы.
        global LAST_TOTAL_FOUND
        if report.get("total_count") is not None:
            LAST_TOTAL_FOUND = report["total_count"]
        if not cards:
            if stats is not None:
                stats["reached_end"] = True
                stats["page_reports"].append(report)
                stats["page_elapsed_sec"] += time.monotonic() - started
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
                    report["rejected"]["missing_price"] = report["rejected"].get("missing_price", 0) + 1
                    continue
                price = int(digits)
                if price < 5_000_000:
                    report["rejected"]["below_minimum_price"] = report["rejected"].get("below_minimum_price", 0) + 1
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
                # выдачи, если у объявления есть фото,
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
                report["accepted_ids"].append(lid)
            except Exception:
                report["rejected"]["card_parse_error"] = report["rejected"].get("card_parse_error", 0) + 1
                continue

        # Unidentified/unparsed cards are a parse failure, not harmless
        # consumer exclusions. Their page must be retried after repair.
        parsed = len(report["accepted_ids"]) + sum(report["rejected"].values())
        if parsed != len(cards) or report["rejected"].get("card_parse_error"):
            report["error"] = "incomplete_card_parse"
            if stats is not None:
                stats["pages_ok"] -= 1
                stats["pages_failed"] += 1
        if stats is not None:
            stats["page_reports"].append(report)
            stats["page_elapsed_sec"] += time.monotonic() - started
            stats["reached_end"] = bool(report.get("terminal_reason") and not report.get("error"))
        if report.get("error") or report.get("terminal_reason"):
            break

    logger.info("apt_parser: got %d listings from %d pages", len(listings), max_pages)
    return listings


async def analyze_apartments(city="astana", max_pages=5, start_page=1,
                              max_price=None, stats: dict | None = None):
    """Full pipeline over the unrestricted sale search by default.

    max_price is an explicit optional filter; ordinary/deep/full sweeps pass
    no cap. stats reports page failures/end-of-search to full_sweep.py.
    """
    from collections import defaultdict

    # 1. Build rental index
    rental_idx = None  # теперь используем rental_index из PostgreSQL

    # 2. Parse sales
    sales = await parse_apartments_for_sale(city, max_pages=max_pages, start_page=start_page,
                                             max_price=max_price, stats=stats)

    # One read supplies both authoritative stored identity and detail-fetch
    # eligibility. A card's stale text must not replace an already resolved FK.
    known_sales = {}
    ids_on_page = [r['id'] for r in sales if r.get('id')]
    if ids_on_page:
        try:
            from bot.db.pg import fetch as _pg_fetch
            known_rows = await _pg_fetch(
                'SELECT id,price,lat,complex_id,complex_resolution,house_attribution,found_at, '
                'source_details_checked_at,source_details_attempted_at '
                'FROM apartment_listings WHERE id=ANY($1::text[])', ids_on_page)
            known_sales = {row['id']: dict(row) for row in known_rows}
        except Exception:
            logger.warning('apt_parser: stored sale identity unavailable; rental identity remains unassigned')

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
        stored = known_sales.get(s.get('id'), {})
        complex_id = stored.get('complex_id') or s.get('complex_id')
        if (stored.get('complex_resolution') in ('geo', 'unbound')
                or stored.get('house_attribution') in ('geo', 'address_geo')):
            complex_id, complex_name = None, None
        rent_data = await lookup_rental_estimate(
            city=city,
            district=d or None,
            complex_name=complex_name or None,
            rooms=rooms,
            prop_type="apartment",
            complex_id=complex_id,
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
            # Configurable asking-price scenario, not achieved rent or a
            # confirmed tax regime. Missing/invalid assumptions hide net.
            from bot.core.rental_yield import NetYieldParams, monthly_yield_scenario
            from bot.db import settings as rental_settings
            try:
                scenario = monthly_yield_scenario(s["price"], rent, NetYieldParams.from_settings(rental_settings.get))
            except (ValueError, TypeError, OverflowError):
                scenario = None
            s["net_yield_pct"] = scenario["net_yield_pct"] if scenario else None
            s["est_rent"] = rent
            s["payback_years"] = round(s["price"] / (rent * 12), 1)
            s["rent_source"] = f"{rent_data.get('level','?')}, n={rent_data.get('sample_count',0)}"
        else:
            s["yield_pct"] = None
            s["net_yield_pct"] = None
            s["est_rent"] = None
            s["payback_years"] = None
            s["rent_source"] = None

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

    if stats and stats.get("pages_failed"):
        logger.warning("apt_parser: search failed; no detail requests in this cycle")
        return results

    from bot.db import settings as _app_settings
    detail_batch = _app_settings.get_int("DETAIL_FETCH_BATCH", 15)

    # Price/coordinates alone cannot establish source freshness. Refresh
    # unchanged details after 72h, oldest actual check/attempt first. An
    # unsuccessful attempt rotates the queue without claiming success.
    from bot.core.sale_detail_refresh import detail_candidates, observed_detail_values
    detail_now = datetime.now(timezone.utc)
    all_candidates = [r for r in results if not r.get("details_fetched")]
    candidates = detail_candidates(all_candidates, known_sales, now=detail_now)
    skipped = len(all_candidates) - len(candidates)
    logger.info("apt_parser: %d объявлений на странице, %d требуют detail-fetch "
                "(возраст/новые/цена/координаты, FIFO), %d пока не требуют проверки",
                len(all_candidates), len(candidates), skipped)
    DETAIL_FETCH_STATS['total_seen'] += len(all_candidates)
    DETAIL_FETCH_STATS['needs_fetch'] += len(candidates)
    DETAIL_FETCH_STATS['skipped'] += skipped

    budget = max(0, detail_batch)
    if stats is not None and type(stats.get("_detail_budget_remaining")) is int:
        budget = min(budget, max(0, stats["_detail_budget_remaining"]))
    if stats is not None:
        stats["_detail_budget_remaining"] = budget
    to_fetch = candidates[:budget]

    from bot.core.apartment_details import (
        fetch_apartment_details, detail_fetch_cooldown_remaining, REQUEST_COUNTS as detail_requests,
    )
    for r in to_fetch:
        if budget <= 0:
            break
        cooldown = detail_fetch_cooldown_remaining()
        if cooldown:
            logger.info("apt_parser: detail-fetch batch paused for %.0f min after "
                        "Krisha block; remaining candidates deferred", cooldown / 60)
            break
        url = r.get("url", "")
        if url:
            logger.info("fetching details for %s (prelim_rank=%.2f)", r["id"], r["_prelim_rank"])
            await asyncio.sleep(random.uniform(8.0, 15.0))  # пауза чтобы не блокировали
            before = detail_requests["detail"]
            cancelled = None
            actual_request = False
            try:
                details = await fetch_apartment_details(url)
            except asyncio.CancelledError as exc:
                cancelled = exc
                details = None
            except Exception as exc:
                logger.warning("apt_parser: details failed for %s (%s)", r["id"], type(exc).__name__)
                details = None
            finally:
                if detail_requests["detail"] > before:
                    actual_request = True
                    r["_source_details_attempted_at"] = datetime.now(timezone.utc).isoformat()
                    budget = max(0, budget - (detail_requests["detail"] - before))
                    if stats is not None:
                        stats["_detail_budget_remaining"] = budget
            if cancelled is not None:
                # The caller must persist attempts-only before propagating
                # cancellation; no catalog/save/success receipt is implied.
                cancelled.sale_detail_attempts = {
                    item["id"]: item["_source_details_attempted_at"]
                    for item in results if item.get("_source_details_attempted_at")}
                raise cancelled
            if (actual_request and details and details.get("source_detail_valid") is True
                    and not details.get("is_archived")):
                r.update(details)
                r["details_fetched"] = True
                r["_source_details_checked_at"] = datetime.now(timezone.utc).isoformat()
                r["_source_detail_values"] = observed_detail_values(details)
                # Настоящий скор посчитается в следующем проходе
                # deal_score.apply_deal_scores() — теперь, когда есть координаты.


    results.sort(key=lambda x: x["_prelim_rank"], reverse=True)
    return results
