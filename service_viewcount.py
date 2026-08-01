"""
Фоновый сервис: реальное число просмотров объявления с krisha.kz.

Почему отдельный сервис, а не часть apartment_details.py (обычный httpx):
статичный HTML отдаёт пустой <span class="nb-views-text">, а настоящее
число возвращает внутренний микросервис Крыши (/ms/views/krisha/live/{id}/)
только настоящему браузеру с его JS/фингерпринтом — httpx получает в ответ
{"status":"ok"} без данных (проверено, см. комментарий в apartment_details.py).
Playwright реально рендерит страницу в headless Chromium, поэтому этот вызов
проходит и число появляется прямо в тексте страницы:
"Объявление посмотрели N раз с DD месяца".

Осознанно медленно и маленькими батчами — открытие полноценного браузера на
объявление тяжелее обычного httpx-запроса, плюс это отдельный, more заметный
паттерн трафика для Крыши, так что общий бюджет запросов держим скромным.

Запуск: отдельный systemd-юнит krisha-viewcount.service (см. README/INSTALL).
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import sys

sys.path.insert(0, ".")

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("viewcount_service")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

_VIEWS_RE = re.compile(r"посмотрел\w*\s+([\d\s]+)\s+раз", re.IGNORECASE)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9",
}


async def fetch_view_count(browser, url: str) -> int | None:
    page = await browser.new_page(extra_http_headers=_HEADERS)
    try:
        await page.goto(url, wait_until="networkidle", timeout=25000)
        await page.wait_for_timeout(1500)  # даём время JS-вызову к /ms/views/... отработать
        text = await page.inner_text("body")
        m = _VIEWS_RE.search(text)
        if not m:
            return None
        digits = re.sub(r"\s", "", m.group(1))
        return int(digits) if digits else None
    except Exception as e:
        log.warning("fetch_view_count failed %s: %s", url, e)
        return None
    finally:
        await page.close()


async def run_cycle(browser) -> dict:
    from bot.db import settings as app_settings
    from bot.db.pg import fetch as pg_fetch, execute as pg_exec

    await app_settings.load()
    batch = app_settings.get_int("VIEWCOUNT_BATCH", 20)
    delay_min = app_settings.get_float("VIEWCOUNT_DELAY_MIN", 6.0)
    delay_max = app_settings.get_float("VIEWCOUNT_DELAY_MAX", 12.0)
    min_age_hours = app_settings.get_float("VIEWCOUNT_MIN_AGE_HOURS", 20.0)

    rows = await pg_fetch("""
        SELECT id, url FROM apartment_listings
        WHERE is_active IS NOT FALSE AND url IS NOT NULL
          AND (views_count_updated_at IS NULL
               OR views_count_updated_at < now() - ($1 || ' hours')::interval)
        ORDER BY views_count_updated_at ASC NULLS FIRST, first_seen DESC
        LIMIT $2
    """, str(min_age_hours), batch)

    updated = 0
    for i, r in enumerate(rows):
        if i:
            await asyncio.sleep(random.uniform(delay_min, delay_max))
        n = await fetch_view_count(browser, r["url"])
        if n is not None:
            await pg_exec(
                "UPDATE apartment_listings SET views_count=$2, views_count_updated_at=now() WHERE id=$1",
                r["id"], n)
            updated += 1
        else:
            # Отмечаем попытку, даже неудачную — иначе один и тот же
            # неудачный URL будет в начале очереди на каждый цикл.
            await pg_exec(
                "UPDATE apartment_listings SET views_count_updated_at=now() WHERE id=$1",
                r["id"])
    log.info("viewcount cycle: %d/%d обновлено", updated, len(rows))
    return {"attempted": len(rows), "updated": updated}


async def main() -> None:
    from bot.db.pg import init_pool, execute as pg_exec
    from playwright.async_api import async_playwright

    await init_pool(DATABASE_URL)
    await pg_exec("ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS views_count_updated_at TIMESTAMPTZ")
    log.info("=== Viewcount service started ===")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        while True:
            try:
                await run_cycle(browser)
            except Exception as e:
                log.error("viewcount cycle error: %s", e, exc_info=True)
            await asyncio.sleep(random.uniform(50 * 60, 70 * 60))  # раз в ~1 час ± джиттер


if __name__ == "__main__":
    asyncio.run(main())
