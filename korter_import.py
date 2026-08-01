"""
Импорт данных о ЖК Астаны с korter.kz: класс жилья, застройщик, цена/м², год.

ВАЖНО про пагинацию: ?page=N НЕ работает через обычный GET — Korter грузит
страницы 2+ через JS/XHR API, обычным httpx их не достать. Поэтому источник
данных — не общий список (263 ЖК), а 4 страницы фильтра по классу жилья
(korter.kz/новостройки-астаны-{эконом,комфорт,бизнес,элит}-класса), которые
ЯВЛЯЮТСЯ отдельными server-rendered страницами с первой порцией ЖК (15-20
каждая, ~60-80 суммарно). Этого достаточно чтобы протегировать крупных
игроков (BI Group, NAK, Tumar Group, Beles...) классом жилья и застройщиком.
Полное покрытие 263 ЖК потребует браузерной автоматизации (Playwright) —
отдельная задача, если понадобится больше данных.

Запуск на сервере:
    cd ~/krisha_bot && venv/bin/python korter_import.py --test    # посмотреть
    cd ~/krisha_bot && venv/bin/python korter_import.py           # записать в БД

Матчинг: по нормализованному имени ЖК против таблицы complexes.
Вежливость: пауза 3-5 сек между запросами, всего 5 запросов за прогон.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import re
import sys

import httpx
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("korter")

BASE = "https://korter.kz"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9",
}

# (URL, класс жилья или None) — район/класс фильтры Korter являются
# ОТДЕЛЬНЫМИ server-rendered страницами (проверено вручную), не JS-пагинацией.
# Комбинация класса + района + общего списка даёт максимальное покрытие за
# один HTTP-прогон без браузерной автоматизации.
SOURCES = [
    (f"{BASE}/новостройки-астаны-эконом-класса", "эконом"),
    (f"{BASE}/новостройки-астаны-комфорт-класса", "комфорт"),
    (f"{BASE}/новостройки-астаны-бизнес-класса", "бизнес"),
    (f"{BASE}/новостройки-астаны-элит-класса", "элит"),
    (f"{BASE}/новостройки-астаны-есильский-район", None),
    (f"{BASE}/новостройки-астаны-алматинский-район", None),
    (f"{BASE}/новостройки-астаны-сарыаркинский-район", None),
    (f"{BASE}/новостройки-астаны-байконурский-район", None),
    (f"{BASE}/новостройки-астаны", None),
]

_DISTRICTS = ["Есильский", "Алматинский", "Байконурский", "Сарыаркинский",
              "Сарыарка", "Нуринский", "Нура", "Косшы"]
_ADDR_MARK = re.compile(r"(ул\.|просп\.|пр\.|мкр\.?|ш\.|VIP-городок|вдоль)")
_PRICE_FROM = re.compile(r"от[\s\u200d]*([\d\s\u200d]{5,}?)\s*₸")
_PRICE_M2 = re.compile(r"([\d\s\u200d]{5,}?)\s*₸\s*за\s*м2")
_NAV_HREF_SKIP = re.compile(
    r"/(новостройки-|продажа-|ипотека|риелторы|застройщики|коттеджные|"
    r"сданные-|preview|message|cookies-policy|privacy-policy|application-terms|"
    r"terms-of-service)"
)


from bot.core.site_enrichment import norm_name, save_enrichment


def _dedupe_name(raw: str) -> str:
    """'ЖК TestЖК Test' -> 'ЖК Test' (имя дублируется в разметке)."""
    raw = raw.replace("Акция", "").strip()
    n = len(raw)
    for half in (n // 2, (n + 1) // 2):
        if half >= 3 and raw[:half] == raw[half:2 * half]:
            return raw[:half].strip()
    return raw.strip()


def _parse_number(s: str) -> int | None:
    digits = re.sub(r"[^\d]", "", s)
    return int(digits) if digits else None


def parse_listing_anchor(a, base_url: str) -> dict | None:
    href = a.get("href", "")
    if not href or _NAV_HREF_SKIP.search(href):
        return None
    text = a.get_text(strip=True)
    if "₸" not in text and "Цена по запросу" not in text:
        return None  # не карточка ЖК

    addr_m = _ADDR_MARK.search(text)
    name_raw = text[:addr_m.start()] if addr_m else text[:80]
    name = _dedupe_name(name_raw)
    if not name or len(name) < 2:
        return None

    district = next((d for d in _DISTRICTS if d in text), None)
    pf_m = _PRICE_FROM.search(text)
    pm2_m = _PRICE_M2.search(text)

    url = href if href.startswith("http") else base_url + href
    return {
        "name": name,
        "district": district,
        "price_from": _parse_number(pf_m.group(1)) if pf_m else None,
        "price_m2": _parse_number(pm2_m.group(1)) if pm2_m else None,
        "url": url,
    }


def parse_page(html: str, housing_class: str | None) -> dict[str, dict]:
    soup = BeautifulSoup(html, "html.parser")
    anchors = soup.find_all("a", href=True)
    found: dict[str, dict] = {}

    i = 0
    while i < len(anchors):
        entry = parse_listing_anchor(anchors[i], BASE)
        if entry:
            # Застройщик — обычно следующая <a>, короткая, без ₸ и известных маркеров
            developer = None
            for j in range(i + 1, min(i + 3, len(anchors))):
                dtext = anchors[j].get_text(strip=True)
                dhref = anchors[j].get("href", "")
                if (dtext and "₸" not in dtext and dtext not in ("Позвонить", "Написать")
                        and not _NAV_HREF_SKIP.search(dhref) and len(dtext) < 60):
                    developer = dtext
                    break
                if "₸" in dtext or parse_listing_anchor(anchors[j], BASE):
                    break  # следующая карточка началась, застройщика не было
            if housing_class:
                entry["housing_class"] = housing_class
            if developer:
                entry["developer"] = developer
            key = norm_name(entry["name"])
            if key:
                found.setdefault(key, {}).update(entry)
        i += 1
    return found


async def fetch_all(test: bool) -> dict[str, dict]:
    all_found: dict[str, dict] = {}
    async with httpx.AsyncClient(headers=HEADERS, timeout=30.0,
                                 follow_redirects=True) as client:
        for url, hclass in SOURCES:
            try:
                resp = await client.get(url)
            except Exception as e:
                log.warning("fetch %s failed: %s", url, e)
                continue
            if resp.status_code != 200:
                log.warning("%s -> %s", url, resp.status_code)
                continue
            page_found = parse_page(resp.text, hclass)
            log.info("%s (%s): %d ЖК", url, hclass or "общий список", len(page_found))
            for k, v in page_found.items():
                all_found.setdefault(k, {}).update(v)
            await asyncio.sleep(random.uniform(3, 5))
    return all_found


async def save_to_db(found: dict) -> None:
    await save_enrichment(found, "korter", set_housing_class=True)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="показать без записи")
    args = parser.parse_args()

    found = await fetch_all(args.test)
    if not found:
        log.error("Ничего не собрано. Возможно korter изменил разметку — "
                  "нужно посмотреть /tmp/korter.html свежим взглядом.")
        sys.exit(1)

    with_class = sum(1 for d in found.values() if d.get("housing_class"))
    with_dev = sum(1 for d in found.values() if d.get("developer"))
    log.info("Итого уникальных ЖК: %d, с классом: %d, с застройщиком: %d",
             len(found), with_class, with_dev)

    if args.test:
        for key, d in sorted(found.items())[:25]:
            log.info("  %-35s класс=%-8s застр=%-25s район=%-12s цена/м²=%s",
                     d.get("name", key)[:35], d.get("housing_class") or "-",
                     (d.get("developer") or "-")[:25], d.get("district") or "-",
                     d.get("price_m2") or "-")
        log.info("--test: в БД НЕ записано. Запусти без --test для записи.")
        return

    await save_to_db(found)


if __name__ == "__main__":
    asyncio.run(main())
