"""
Фоновый сервис: обновление данных ЖК с korter.kz (класс жилья, застройщик,
район, цена/м²) с интервалом 1-4 часа — тестируем, не даёт ли Korter банов
на такой частоте. При проблемах (403/429/капча) интервал можно увеличить
через настройку KORTER_INTERVAL_HOURS в /admin/settings в будущем; сейчас
захардкожен как разумный дефолт.

Логирует в korter.log (см. krisha-korter.service).
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import sys

sys.path.insert(0, ".")

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("korter_service")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")
MIN_INTERVAL_H = 1
MAX_INTERVAL_H = 4


async def run_cycle():
    from korter_import import fetch_all, save_to_db

    log.info("=== Korter cycle start ===")
    try:
        found = await fetch_all(test=False)
        if not found:
            log.warning("Korter cycle: ничего не собрано (возможно, разметка изменилась)")
            return
        await save_to_db(found)
        log.info("=== Korter cycle done: %d ЖК обработано ===", len(found))
    except Exception as e:
        log.error("Korter cycle failed: %s", e, exc_info=True)


async def main():
    from bot.db.pg import init_pool

    await init_pool(DATABASE_URL)
    log.info("=== Korter service started ===")

    while True:
        try:
            await run_cycle()
        except Exception as e:
            log.error("Korter loop error: %s", e, exc_info=True)

        sleep_h = random.uniform(MIN_INTERVAL_H, MAX_INTERVAL_H)
        log.info("Sleeping %.1f hours...\n", sleep_h)
        await asyncio.sleep(sleep_h * 3600)


if __name__ == "__main__":
    asyncio.run(main())
