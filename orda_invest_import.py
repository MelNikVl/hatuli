"""
Новостройки Orda Invest (orda-invest.kz) — карточки квартир по ЖК Астаны.

В отличие от BI Group это Nuxt.js SSR-сайт — карточки квартир уже лежат в
HTML первого ответа (server-rendered), никакого отдельного JSON API дёргать
не нужно: обычный httpx + BeautifulSoup, без Playwright.

Структура (проверена вручную curl'ом):
  - /complexes/{id} — карточка ЖК. id 1..15 подряд, кроме 7 ("О компании" —
    не ЖК, служебная страница). Список автообнаруживается (см.
    discover_complex_ids) — на случай новых ЖК без правки кода.
  - Список корпусов ЖК зашит в hydration-блоб `window.__NUXT__` прямо в HTML
    этой же страницы: `complexBlocks:[{id:67,name:"квартиры блок 1",...},
    ...]` — простым regex'ом достаём id корпусов (имя не нужно, оно и так
    есть в каждой карточке квартиры).
  - /complexes/{id}?block_id={bid}&types=living&page={n} — карточки квартир
    этого корпуса (.complex-offer-list-card), пагинация обычная ?page=N,
    останавливаемся когда страница вернула 0 карточек.

Статус: сайт публикует ТОЛЬКО активные (доступные) квартиры — ни одного
"забронировано"/"продано"-класса на карточках не встретилось (проверено).
Значит все собранные юниты — 'available'; ушедшие в продажу просто
пропадают из выдачи между обходами (единая логика в newbuild_common.py).

Не удалось найти на этих страницах: точный адрес/координаты ЖК и явный
"срок сдачи" (данные явно есть на сайте где-то, но не в этой части
разметки/state) — оставлено на потом, is_newbuild считается TRUE по
умолчанию (нет completion_year), геокодинга/маркера на карте для этих
ЖК пока не будет, только карточки квартир и данные в разделе "Новостройки".

Запуск:
    venv/bin/python orda_invest_import.py --test --limit 2
    venv/bin/python orda_invest_import.py
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import random
import re
import sys

import httpx
from bs4 import BeautifulSoup

sys.path.insert(0, ".")

from dotenv import load_dotenv
load_dotenv()

from newbuild_common import ComplexData, UnitData, run_source

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("orda_invest")

BASE = "https://orda-invest.kz"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9",
}
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

SOURCE = "orda_invest"
DEVELOPER_NAME = "Orda Invest"
DEVELOPER_WEBSITE = "https://orda-invest.kz"
DEVELOPER_PHONE = None  # не нашли на страницах шахматки — заполнить вручную в /admin/developer/{id}

_TITLE_RE = re.compile(r"<title>Orda \| ([^<]*)</title>")
_ROOMS_RE = re.compile(r"(\d+)-комн")
_AREA_RE = re.compile(r"([\d.]+)\s*м²")
_FLOOR_RE = re.compile(r"(\d+)\s*из\s*(\d+)")
_PRICE_RE = re.compile(r"([\d\s]+)\s*₸")
_NUM_RE = re.compile(r"№\s*(\S+)")


async def _pause() -> None:
    await asyncio.sleep(random.uniform(0.3, 0.7))


async def discover_complex_ids(client: httpx.AsyncClient, max_id: int = 40,
                                stop_after_misses: int = 6) -> list[int]:
    """id 1..N подряд — пробуем по возрастающей, останавливаемся после
    нескольких промахов подряд (не-ЖК страница/404), чтобы новые ЖК не
    требовали правки кода, но и не пробивать диапазон в бесконечность."""
    ids: list[int] = []
    misses = 0
    for i in range(1, max_id + 1):
        try:
            resp = await client.get(f"{BASE}/complexes/{i}")
        except Exception as e:
            log.warning("complexes/%d: %s", i, e)
            misses += 1
            if misses >= stop_after_misses:
                break
            continue
        title_m = _TITLE_RE.search(resp.text)
        title = title_m.group(1).strip() if title_m else ""
        if resp.status_code == 200 and title and title != "О компании":
            ids.append(i)
            misses = 0
        else:
            misses += 1
            if misses >= stop_after_misses:
                break
        await _pause()
    return ids


def extract_block_ids(html: str) -> list[int]:
    idx = html.find("complexBlocks:[")
    if idx == -1:
        return []
    window = html[idx:idx + 5000]
    end = window.find("],banners")
    if end != -1:
        window = window[:end]
    return sorted(set(int(m) for m in re.findall(r"\{id:(\d+),name:", window)))


def _parse_card(card, complex_id: int, block_id: int) -> UnitData | None:
    h6 = card.select_one("h6")
    if not h6:
        return None
    title_txt = h6.get_text(" ", strip=True)
    rooms_m = _ROOMS_RE.search(title_txt)
    num_m = _NUM_RE.search(title_txt)
    if not num_m:
        return None

    items = {}
    for item in card.select(".complex-offer-list-card__item"):
        label = item.find("span")
        val = item.find("p")
        if label and val:
            items[label.get_text(strip=True)] = val.get_text(strip=True)

    area_m = _AREA_RE.search(items.get("Площадь", ""))
    floor_m = _FLOOR_RE.search(items.get("Этаж", ""))
    block_name = items.get("Блок")

    price_el = card.select_one(".complex-offer-list-card__price")
    price = None
    if price_el:
        price_m = _PRICE_RE.search(price_el.get_text(" ", strip=True))
        if price_m:
            price = int(re.sub(r"\s", "", price_m.group(1)))

    img = card.select_one(".image-block__image")
    photo = img.get("src") if img else None
    if photo and photo.startswith("/"):
        photo = BASE + photo

    return UnitData(
        source_unit_id=f"{complex_id}-{block_id}-{num_m.group(1)}",
        rooms=int(rooms_m.group(1)) if rooms_m else None,
        area=float(area_m.group(1)) if area_m else None,
        floor=int(floor_m.group(1)) if floor_m else None,
        floors_total=int(floor_m.group(2)) if floor_m else None,
        price=price,
        price_per_m2=round(price / float(area_m.group(1))) if price and area_m else None,
        building=block_name,
        section=None,
        layout_photo_url=photo,
        photos=None,
        is_available=True,  # сайт показывает только доступные (см. докстринг)
        deadline=None,
        raw={"title": title_txt, "items": items, "price_text": price_el.get_text() if price_el else None},
    )


async def fetch_block_units(client: httpx.AsyncClient, complex_id: int, block_id: int) -> list[UnitData]:
    units: list[UnitData] = []
    page = 1
    while True:
        resp = await client.get(f"{BASE}/complexes/{complex_id}",
                                 params={"block_id": block_id, "types": "living", "page": page})
        if resp.status_code != 200:
            break
        soup = BeautifulSoup(resp.text, "html.parser")
        cards = soup.select(".complex-offer-list-card")
        if not cards:
            break
        for card in cards:
            u = _parse_card(card, complex_id, block_id)
            if u:
                units.append(u)
        page += 1
        if page > 100:
            log.warning("complex=%d block=%d: остановился на странице %d", complex_id, block_id, page)
            break
        await _pause()
    return units


async def fetch_complex(client: httpx.AsyncClient, complex_id: int) -> ComplexData | None:
    resp = await client.get(f"{BASE}/complexes/{complex_id}")
    if resp.status_code != 200:
        return None
    title_m = _TITLE_RE.search(resp.text)
    name = title_m.group(1).strip() if title_m else f"Orda Invest #{complex_id}"
    block_ids = extract_block_ids(resp.text)
    if not block_ids:
        log.warning("complex=%d (%s): корпуса не найдены", complex_id, name)
        return None

    all_units: list[UnitData] = []
    for bid in block_ids:
        await _pause()
        units = await fetch_block_units(client, complex_id, bid)
        all_units.extend(units)
    log.info("[%s] %d корпусов, %d квартир", name, len(block_ids), len(all_units))
    if not all_units:
        return None
    return ComplexData(source_id=str(complex_id), name=name, address=None,
                       housing_class=None, units=all_units)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="показать без записи в БД")
    parser.add_argument("--limit", type=int, default=None, help="только первые N ЖК (для проверки)")
    args = parser.parse_args()

    async with httpx.AsyncClient(headers=HEADERS, timeout=30.0) as client:
        ids = await discover_complex_ids(client)
        if args.limit:
            ids = ids[:args.limit]
        log.info("Найдено ЖК: %s", ids)
        complexes: list[ComplexData] = []
        for cid in ids:
            cx = await fetch_complex(client, cid)
            if cx:
                complexes.append(cx)
            await _pause()

    total_units = sum(len(c.units) for c in complexes)
    log.info("Итого: %d ЖК, %d квартир", len(complexes), total_units)

    if not complexes:
        log.error("Ничего не собрано — возможно Orda Invest поменяли вёрстку.")
        sys.exit(1)

    if args.test:
        for c in complexes:
            log.info("  %-30s %4d квартир", c.name[:30], len(c.units))
        log.info("--test: в БД НЕ записано.")
        return

    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    try:
        stats = await run_source(SOURCE, DEVELOPER_NAME, DEVELOPER_WEBSITE, DEVELOPER_PHONE, complexes)
        log.info("=== Итог: ЖК=%d новых_юнитов=%d обновлено=%d продано=%d изменений_цены=%d ===",
                  stats.get("complexes", 0), stats.get("units_new", 0), stats.get("units_seen", 0),
                  stats.get("units_sold", 0), stats.get("price_changes", 0))
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
