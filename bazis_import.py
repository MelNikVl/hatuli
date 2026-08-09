"""
Новостройки BAZIS-A (sales.bazis.kz) — каталог квартир по ЖК Астаны.

В отличие от BI Group/Orda Invest здесь нет ни открытого JSON API, ни SSR —
чистый Vue SPA с бесконечной подгрузкой карточек по скроллу. Пришлось
поднимать headless-браузер (Playwright, см. service_viewcount.py — тот же
паттерн в проекте уже есть для похожей задачи).

Структура (проверена вручную в браузере):
  - /flats — каталог квартир по ВСЕЙ сети BAZIS-A (несколько городов сразу).
    Фильтр "Город" — обычные <a> в DOM (не настоящий select), просто
    активная ссылка получает class="active"; кликаем по "Астана".
  - Карточка квартиры — div.flat-item:
      a.item-link[href="/flats/{uuid}"]  — стабильный id квартиры
      .item-img img[src]                 — планировка (фото)
      .item-info li (.left + .right)     — Объект/Площадь/Цена м²/
                                            Стоимость/Этаж/Кв. №/Пятно
    "Пятно" — судя по всему номер корпуса/пятна застройки, используем как
    building. Явного этажности дома (floors_total) на карточке нет.
  - Подгрузка — infinite scroll (проверено: после первой отрисовки в DOM
    всего ~30 карточек из "Найдено квартир: N"), поэтому скроллим до
    стабилизации количества .flat-item на странице.

Статус: как и у остальных источников, сайт показывает только то, что можно
купить — нет отдельного визуального признака "забронировано"/"продано" на
карточке, поэтому всё найденное = 'available'; пропавшее между обходами
уходит в 'sold' (общая логика в newbuild_common.py).

Не нашли на этой странице (в отличие от Orda Invest): точный адрес и срок
сдачи по каждому ЖК — geocoding/completion_year пока не заполняются,
завести можно отдельным заходом на страницу конкретного ЖК позже.

Запуск:
    venv/bin/python bazis_import.py --test --limit-scroll 5   # для проверки, без записи
    venv/bin/python bazis_import.py
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys

sys.path.insert(0, ".")

from dotenv import load_dotenv
load_dotenv()

from newbuild_common import ComplexData, UnitData, run_source

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bazis")

BASE = "https://sales.bazis.kz"
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

SOURCE = "bazis"
# Кириллическая "А" — в базе уже есть застройщик под этим написанием
# (id=71, с сайтом bazis.kz и алиасами) из более раннего обогащения
# korter/homsters; латинская "BAZIS-A" была бы визуально неотличимым, но
# отдельным дублем (см. ensure_developer — матчинг точный посимвольно,
# только case-insensitive, "A"≠"А" для него).
DEVELOPER_NAME = "BAZIS-А"
DEVELOPER_WEBSITE = "https://sales.bazis.kz"
DEVELOPER_PHONE = None  # не нашли на странице каталога — заполнить вручную в /admin/developer/{id}

_HEADERS_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

_EXTRACT_JS = """
() => Array.from(document.querySelectorAll('.flat-item')).map(card => {
  const href = card.querySelector('.item-link')?.getAttribute('href') || '';
  const id = href.split('/').filter(Boolean).pop() || '';
  const img = card.querySelector('.item-img img')?.getAttribute('src') || null;
  const kv = {};
  card.querySelectorAll('.item-info li').forEach(li => {
    const left = li.querySelector('.left')?.textContent.trim();
    const right = li.querySelector('.right')?.textContent.replace(/\\s+/g, ' ').trim();
    if (left) kv[left] = right;
  });
  return {id, img, kv};
})
"""


def _parse_price_per_m2(s: str | None) -> int | None:
    if not s:
        return None
    m = re.search(r"([\d.]+)\s*тг", s)
    return int(m.group(1).replace(".", "")) if m else None


def _parse_total_price(s: str | None) -> int | None:
    if not s:
        return None
    m = re.search(r"([\d.]+)\s*млн", s)
    if m:
        return round(float(m.group(1)) * 1e6)
    m = re.search(r"([\d.]+)\s*тг", s)
    return int(m.group(1).replace(".", "")) if m else None


def _parse_area(s: str | None) -> float | None:
    if not s:
        return None
    m = re.search(r"([\d.]+)\s*м", s)
    return float(m.group(1)) if m else None


def _to_unit_data(raw: dict) -> UnitData | None:
    kv = raw.get("kv") or {}
    if not raw.get("id") or not kv.get("Объект"):
        return None
    area = _parse_area(kv.get("Площадь"))
    price = _parse_total_price(kv.get("Стоимость"))
    floor_s = (kv.get("Этаж") or "").strip()
    return UnitData(
        source_unit_id=raw["id"],
        rooms=None,  # не в .item-info — берём из .item-title отдельно, см. collect_cards
        area=area,
        floor=int(floor_s) if floor_s.isdigit() else None,
        floors_total=None,
        price=price,
        price_per_m2=_parse_price_per_m2(kv.get("Цена м²")),
        building=(kv.get("Пятно") or "").strip() or None,
        section=None,
        layout_photo_url=raw.get("img"),
        photos=None,
        is_available=True,
        deadline=None,
        raw=raw,
    )


async def collect_astana_cards(page, max_scrolls: int, stable_rounds_needed: int = 4) -> list[dict]:
    await page.goto(f"{BASE}/flats", wait_until="networkidle", timeout=45000)
    # Список городов — обычные <a> в DOM, скрытые CSS'ом до hover/клика по
    # текущему значению (не настоящий <select>) — обычный .click() не видит
    # их "visible" (0 opacity/height до раскрытия), поэтому кликаем прямо в
    # JS (минуя проверку видимости Playwright) — надёжнее, чем гадать с
    # hover-эмуляцией по этому конкретному CSS.
    await page.evaluate("""
        () => {
            const links = document.querySelectorAll('.dropwdown-filter-select-city a');
            for (const a of links) { if (a.textContent.trim() === 'Астана') { a.click(); return; } }
        }
    """)
    await page.wait_for_timeout(1500)

    # НЕ infinite scroll (первое впечатление в интерактивной проверке
    # обмануло — скролл просто открывал уже загруженные карточки ниже по
    # экрану) — это обычная кнопка "Загрузить ещё N" (div.pagination-btns a)
    # с числом оставшихся в тексте. Жмём, пока не пропадёт/не останется 0.
    last_count = 0
    stable = 0
    for i in range(max_scrolls):
        clicked = await page.evaluate("""
            () => {
                const btn = document.querySelector('.pagination-btns a');
                if (btn) { btn.click(); return true; }
                return false;
            }
        """)
        if not clicked:
            log.info("  кнопка «Загрузить ещё» пропала — всё подгружено")
            break
        await page.wait_for_timeout(1200)
        count = await page.locator(".flat-item").count()
        if count == last_count:
            stable += 1
            if stable >= stable_rounds_needed:
                log.warning("  count не растёт %d раз подряд, останавливаюсь на %d", stable, count)
                break
        else:
            stable = 0
        last_count = count
        if i % 10 == 0:
            log.info("  подгружено карточек: %d", count)

    log.info("итого карточек в DOM: %d", last_count)
    cards = await page.evaluate(_EXTRACT_JS)

    # rooms — из .item-title (число комнат), .item-info его не содержит
    rooms_by_id = await page.evaluate("""
        () => Array.from(document.querySelectorAll('.flat-item')).reduce((acc, card) => {
            const href = card.querySelector('.item-link')?.getAttribute('href') || '';
            const id = href.split('/').filter(Boolean).pop();
            const rooms = card.querySelector('.rooms')?.textContent.trim();
            if (id) acc[id] = rooms ? parseInt(rooms, 10) : null;
            return acc;
        }, {})
    """)
    for c in cards:
        c["rooms"] = rooms_by_id.get(c["id"])
    return cards


def group_into_complexes(cards: list[dict]) -> list[ComplexData]:
    by_name: dict[str, list[UnitData]] = {}
    for raw in cards:
        u = _to_unit_data(raw)
        if not u:
            continue
        u.rooms = raw.get("rooms")
        name = raw["kv"]["Объект"].strip()
        by_name.setdefault(name, []).append(u)
    return [ComplexData(source_id=name, name=name, address=None, housing_class=None, units=units)
            for name, units in by_name.items()]


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="показать без записи в БД")
    parser.add_argument("--limit-scroll", type=int, default=300,
                        help="макс. число прокруток (по умолчанию хватает на весь каталог)")
    args = parser.parse_args()

    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(user_agent=_HEADERS_UA)
        try:
            from playwright_stealth import Stealth
            await Stealth().apply_stealth_async(page)
        except Exception as e:
            log.warning("playwright-stealth недоступен, работаем без него: %s", e)
        try:
            cards = await collect_astana_cards(page, args.limit_scroll)
        finally:
            await browser.close()

    complexes = group_into_complexes(cards)
    total_units = sum(len(c.units) for c in complexes)
    log.info("Астана: %d ЖК, %d квартир", len(complexes), total_units)
    for c in complexes:
        log.info("  %-30s %4d квартир", c.name[:30], len(c.units))

    if not complexes:
        log.error("Ничего не собрано — возможно BAZIS-A поменяли вёрстку.")
        sys.exit(1)

    if args.test:
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
