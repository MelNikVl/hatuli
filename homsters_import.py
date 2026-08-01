"""
Импорт данных о ЖК Астаны с homsters.kz: застройщик, цена, диапазон площади/
комнат, район.

Структура (проверена на живой странице 12.07.2026): карточка ЖК — это
заголовок-ссылка <a href="/{застройщик-slug}/{жк-slug}">ЖК Имя</a> (имя БЕЗ
дублирования, в отличие от Korter), а район/цена/площадь/комнаты идут РЯДОМ
как обычный текст внутри того же блока карточки, не внутри самой ссылки.
Поэтому: имя берём из текста ссылки, а остальные поля — из текста
ближайшего родительского контейнера, где встречается «м²».

Застройщик в списке явно не пишется, но угадывается по первому сегменту
URL (bi-group-development, bazis, ...) — есть встроенный словарь по
каталогу застройщиков Астаны с этой же страницы.

Пагинация — обычные ссылки /estate/search/astana-and-primary/page{N}
(НЕ JS, в отличие от Korter) — итого ~992 ЖК по Казахстану на 59
страницах общего списка Астаны. По умолчанию читаем первые MAX_PAGES
страниц (дефолт 15 → ~250 ЖК) — регулировать через --pages.

Запуск:
    venv/bin/python homsters_import.py --test
    venv/bin/python homsters_import.py --test --pages 5
    venv/bin/python homsters_import.py
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import random
import re
import sys

import httpx
from bs4 import BeautifulSoup

sys.path.insert(0, ".")
from bot.core.site_enrichment import norm_name, save_enrichment

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("homsters")

BASE = "https://homsters.kz"
LIST_URL = f"{BASE}/estate/search/astana-and-primary"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9",
}

DEFAULT_MAX_PAGES = 15

# Ссылки на карточку ЖК — ровно 2 сегмента пути /{застройщик}/{жк}.
# Эти корни встречаются как первый сегмент у НЕ-карточек — исключаем.
_ROOT_BLACKLIST = {"estate", "static", "personal", "home", "developers",
                   "reviews", "promo", "for-developers", "sitemap", "kz"}

_DISTRICTS = ["Есиль", "Нура", "Алматы", "Сарыарка", "Байконур"]
_PRICE_FROM = re.compile(r"от[\s\u200d]*([\d\s\u200d,]{4,}?)\s*млн\s*тг")
_AREA_RANGE = re.compile(r"(\d+[.,]?\d*)\s*[-–]\s*(\d+[.,]?\d*)\s*м²")
_ROOMS_RANGE = re.compile(r"(\d+)\s*\.{3}\s*(\d+)\+?\s*комн")

# Каталог застройщиков Астаны (слаг из URL -> читаемое имя), собран со
# страницы списка. Неизвестные слаги — эвристика (замена дефисов, title-case).
_DEVELOPER_SLUGS = {
    "bi-group-development": "BI Group",
    "bazis": "BAZIS-А",
    "svoy-dom": "Svoy Dom",
    "nur-astana-kurylys": "NAK",
    "orda-invest": "ORDA INVEST",
    "sensata-group": "Sensata Group",
    "global-expert-development-group": "Global Expert Development",
    "too-g-park": "G-Park",
    "tumar-group": "Tumar Group",
    "ulytau-group": "Ulytau Group",
    "beles": "BELES",
    "too-sat-ns": "SAT-NS",
    "galamat-group": "Galamat Group",
    "shar-kyrylys": "Шар-Құрылыс",
    "mabex-invest": "Mabex Trade LTD",
    "stroiklass": "STROIKLASS",
    "sardar-construction-group": "Sardar Construction Group",
    "investitsionnaya-stroitelnaya-kompaniya-asi": "ASI",
    "astana-servis-stroj-montazh": "Астана Сервис Строй Монтаж",
    "zhsk-zhanuya-invest": "Жануя Инвест ЖСК",
    "nurzher-nurzher": "Nurzher",
}


def _developer_from_slug(slug: str) -> str:
    if slug in _DEVELOPER_SLUGS:
        return _DEVELOPER_SLUGS[slug]
    return slug.replace("-", " ").replace("too ", "").strip().title()


def _num(s: str) -> float | None:
    s = s.replace("\u200d", "").replace(",", ".").replace(" ", "")
    try:
        return float(re.sub(r"[^\d.]", "", s))
    except ValueError:
        return None


def _path_parts(href: str) -> list[str]:
    path = href.split("?")[0].split("#")[0]
    if path.startswith("http"):
        path = re.sub(r"^https?://[^/]+", "", path)
    return [p for p in path.strip("/").split("/") if p]


def _find_info_text(a) -> str:
    """Ближайший родительский контейнер, где уже виден 'м²' (там же цена/район)."""
    node = a
    for _ in range(6):
        node = node.parent
        if node is None:
            break
        t = node.get_text(" ", strip=True)
        if "м²" in t:
            return t
    return a.get_text(" ", strip=True)


def parse_listing_anchor(a) -> dict | None:
    href = a.get("href", "")
    if not href:
        return None
    parts = _path_parts(href)
    if len(parts) != 2 or parts[0] in _ROOT_BLACKLIST:
        return None

    name = a.get_text(strip=True)
    if not name or len(name) < 2 or len(name) > 80:
        return None

    info = _find_info_text(a)
    district = next((d for d in _DISTRICTS if d in info), None)
    pf_m = _PRICE_FROM.search(info)
    area_m = _AREA_RANGE.search(info)
    rooms_m = _ROOMS_RANGE.search(info)
    stage_badge = None
    if "Ход строительства" in info:
        stage_badge = "строится"
    elif "Есть разрешение" in info:
        stage_badge = "есть разрешение"

    return {
        "name": name,
        "district": district,
        "price_from": int(_num(pf_m.group(1)) * 1_000_000) if pf_m else None,
        "area_min": _num(area_m.group(1)) if area_m else None,
        "area_max": _num(area_m.group(2)) if area_m else None,
        "rooms_min": int(rooms_m.group(1)) if rooms_m else None,
        "rooms_max": int(rooms_m.group(2)) if rooms_m else None,
        "developer": _developer_from_slug(parts[0]),
        "stage_badge": stage_badge,
        "url": BASE + "/" + "/".join(parts),
    }


def parse_page(html: str) -> dict[str, dict]:
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, dict] = {}
    for a in soup.find_all("a", href=True):
        entry = parse_listing_anchor(a)
        if entry:
            key = norm_name(entry["name"])
            if key:
                found.setdefault(key, {}).update(entry)
    return found


def _fetch_curl_cffi(url: str) -> tuple[int, str]:
    """
    Homsters отдаёт 403 обычному httpx (анти-бот по TLS-отпечатку).
    curl_cffi с impersonate='chrome' имитирует настоящий браузерный
    TLS-стек и обычно проходит. Установка: venv/bin/pip install curl_cffi
    """
    from curl_cffi import requests as curl_requests
    resp = curl_requests.get(url, headers=HEADERS, impersonate="chrome", timeout=30)
    return resp.status_code, resp.text


async def _fetch(url: str, client: httpx.AsyncClient) -> tuple[int, str]:
    """Сначала curl_cffi (обходит анти-бот), при его отсутствии — httpx."""
    try:
        import curl_cffi  # noqa: F401
        return await asyncio.to_thread(_fetch_curl_cffi, url)
    except ImportError:
        log.warning("curl_cffi не установлен — пробую httpx (вероятен 403). "
                    "Поставь: venv/bin/pip install curl_cffi")
        resp = await client.get(url)
        return resp.status_code, resp.text


async def fetch_all(max_pages: int = DEFAULT_MAX_PAGES) -> dict[str, dict]:
    all_found: dict[str, dict] = {}
    async with httpx.AsyncClient(headers=HEADERS, timeout=30.0,
                                 follow_redirects=True) as client:
        for page in range(1, max_pages + 1):
            url = LIST_URL if page == 1 else f"{LIST_URL}/page{page}"
            try:
                status, text = await _fetch(url, client)
            except Exception as e:
                log.warning("fetch %s failed: %s", url, e)
                break
            if status != 200:
                log.warning("%s -> %s, останавливаюсь", url, status)
                break
            before = len(all_found)
            page_found = parse_page(text)
            for k, v in page_found.items():
                all_found.setdefault(k, {}).update(v)
            log.info("page %d: +%d ЖК (всего %d)", page, len(all_found) - before, len(all_found))
            if len(all_found) == before and page > 1:
                log.info("новых ЖК не прибавилось — похоже, страницы кончились")
                break
            await asyncio.sleep(random.uniform(3, 5))
    return all_found


async def save_to_db(found: dict) -> None:
    await save_enrichment(found, "homsters", set_housing_class=False)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--pages", type=int, default=DEFAULT_MAX_PAGES)
    args = parser.parse_args()

    found = await fetch_all(args.pages)
    if not found:
        log.error("Ничего не собрано — возможно разметка homsters снова изменилась.")
        sys.exit(1)

    with_dev = sum(1 for d in found.values() if d.get("developer"))
    with_price = sum(1 for d in found.values() if d.get("price_from"))
    log.info("Итого уникальных ЖК: %d, с застройщиком: %d, с ценой: %d",
             len(found), with_dev, with_price)

    if args.test:
        for key, d in sorted(found.items())[:25]:
            log.info("  %-28s район=%-10s застр=%-22s цена от=%-12s пл=%s-%s комн=%s-%s%s",
                     d.get("name", key)[:28], d.get("district") or "-",
                     (d.get("developer") or "-")[:22], d.get("price_from") or "-",
                     d.get("area_min") or "-", d.get("area_max") or "-",
                     d.get("rooms_min") or "-", d.get("rooms_max") or "-",
                     f" [{d['stage_badge']}]" if d.get("stage_badge") else "")
        log.info("--test: в БД НЕ записано.")
        return

    await save_to_db(found)


if __name__ == "__main__":
    asyncio.run(main())
