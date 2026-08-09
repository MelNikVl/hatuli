#!/usr/bin/env python3
"""
Сервис парсинга АРЕНДЫ.
Парсит krisha.kz/arenda/ каждые 5-15 минут (по одной странице).
После каждого полного прохода пересчитывает rental_index и синкает в Google Sheets.

Запуск:  python service_rental.py
Логи:    rental.log
"""
import asyncio
import logging
import os
import random

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("rental.log", encoding="utf-8", errors="replace"),
    ],
)
log = logging.getLogger("rental_service")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")
MAX_PAGES_PER_TYPE = 5


async def main():
    from bot.db.pg import init_pool
    from bot.core.rental_parser import (
        _fetch_page, save_rental_listings,
        rebuild_rental_index, BASE_URL, DEFAULT_HEADERS, RENTAL_PATHS
    )
    from bot.core.sheets_sync_rental import sync_rental_to_sheets
    import httpx

    await init_pool(DATABASE_URL)
    log.info("=== Rental service started ===")

    paths = list(RENTAL_PATHS.items())
    path_idx = 0
    page_num = 1
    full_passes = 0

    while True:
        try:
            path, prop_type = paths[path_idx]
            url = BASE_URL + path + (f"?page={page_num}" if page_num > 1 else "")
            log.info("[%s] page %d → %s", prop_type, page_num, url)

            async with httpx.AsyncClient(
                headers=DEFAULT_HEADERS, follow_redirects=True, timeout=20
            ) as client:
                listings = await _fetch_page(client, url, prop_type)

            if listings:
                saved = await save_rental_listings(listings)
                log.info("  saved %d/%d", saved, len(listings))
            else:
                log.info("  empty page — moving to next type")

            # Переходим к следующей странице или типу
            page_num += 1
            if page_num > MAX_PAGES_PER_TYPE or not listings:
                path_idx = (path_idx + 1) % len(paths)
                page_num = 1

                if path_idx == 0:
                    full_passes += 1
                    log.info("--- Full pass #%d complete → rebuilding index ---", full_passes)
                    await rebuild_rental_index()

                    if full_passes % 3 == 0:
                        try:
                            await sync_rental_to_sheets()
                            log.info("Google Sheets: Аренда synced")
                            from bot.db import settings as app_settings
                            from datetime import datetime, timezone
                            await app_settings.set("SHEETS_RENTAL_SYNCED_AT",
                                                   datetime.now(timezone.utc).isoformat())
                        except Exception as e:
                            log.warning("Sheets sync failed: %s", e)

        except Exception as e:
            log.error("Rental loop error: %s", e, exc_info=True)
        # === Дедупликация аренды ===
        try:
            from bot.core.dedup_listings import deduplicate_rental_listings
            dup_count = await deduplicate_rental_listings()
            if dup_count:
                log.info("Deduplicated %d rental listings", dup_count)
        except Exception as e:
            log.warning("Rental deduplication failed: %s", e)

        # === Бэкфилл привязки аренды: ЖК (офиц. блок Крыши) / координаты / адрес ===
        try:
            from bot.db import settings as _st_r
            from bot.core.rental_parser import backfill_rental_details
            await _st_r.load()
            rb = _st_r.get_int("RENTAL_BACKFILL_BATCH", 8)
            if rb > 0:
                res = await backfill_rental_details(rb)
                if res.get("checked"):
                    log.info("Rental backfill: %s", res)
        except Exception as e:
            log.warning("Rental backfill failed: %s", e)

        # === Проверка архивности объявлений аренды ===
        try:
            from bot.db import settings as _st_arch
            from bot.core.archive_check import check_archived_rentals
            await _st_arch.load()
            archive_batch = _st_arch.get_int("RENTAL_ARCHIVE_CHECK_BATCH", 60)
            if archive_batch > 0:
                res = await check_archived_rentals(limit=archive_batch)
                log.info("Rental archive check: %s", res)
        except Exception as e:
            log.warning("Rental archive check failed: %s", e)

        # === Привязка аренды по адресу (см. bot.core.rebind.bind_by_address) ===
        # Приоритет ПЕРЕД геопривязкой по близости ниже: точное совпадение
        # нормализованного адреса с адресом уже подтверждённых объявлений
        # ПРОДАЖИ того же ЖК — надёжнее, чем "ближайший по прямой" (см.
        # докстринг rebind.py — та же логика, что убрали для sale-объявлений
        # из-за качества). Правильный порядок: сначала точные сигналы
        # (офиц. блок Крыши в backfill выше, потом адрес), и только если
        # ничего не подтвердилось — угадывание по близости как крайний
        # случай, а не как основной способ привязки.
        try:
            from bot.core.rebind import bind_by_address
            n_addr = await bind_by_address("rental_listings")
            if n_addr:
                log.info("Rental address-bind: %d", n_addr)
        except Exception as e:
            log.warning("Rental address-bind failed: %s", e)

        # === Геопривязка аренды к ближайшему ЖК (≤ ~350 м, без ЖК-улиц) ===
        # КРАЙНИЙ СЛУЧАЙ — только для того, что не подтвердилось выше ни
        # офиц. ссылкой Крыши, ни адресом. Блайнд-угадывание по расстоянию,
        # без текстового подтверждения; для sale-объявлений аналогичная
        # стадия была убрана вовсе (см. rebind.py) — здесь пока оставлена
        # (у аренды меньше альтернативных сигналов), но теперь выполняется
        # ПОСЛЕДНЕЙ, а не единственной.
        try:
            from bot.db.pg import execute as _pex_geo
            await _pex_geo("""
                UPDATE rental_listings r
                SET complex_name = (
                    SELECT c2.name FROM complexes c2
                    WHERE c2.lat IS NOT NULL AND c2.lon IS NOT NULL
                      AND COALESCE(c2.is_street, FALSE) = FALSE
                      AND COALESCE(c2.is_garbage, FALSE) = FALSE
                    ORDER BY (c2.lat - r.lat)^2 + (c2.lon - r.lon)^2
                    LIMIT 1)
                WHERE (r.complex_name IS NULL OR btrim(r.complex_name) = '')
                  AND r.lat IS NOT NULL AND r.lon IS NOT NULL
                  AND (SELECT min((c.lat - r.lat)^2 + (c.lon - r.lon)^2)
                       FROM complexes c
                       WHERE c.lat IS NOT NULL AND c.lon IS NOT NULL
                         AND COALESCE(c.is_street, FALSE) = FALSE
                         AND COALESCE(c.is_garbage, FALSE) = FALSE) < 2.0e-5
            """)
        except Exception as e:
            log.warning("Rental geo-bind failed: %s", e)


        sleep_sec = random.uniform(5 * 60, 15 * 60)
        log.info("Sleeping %.0f min...\n", sleep_sec / 60)
        await asyncio.sleep(sleep_sec)


if __name__ == "__main__":
    asyncio.run(main())
