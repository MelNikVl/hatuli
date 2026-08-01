"""
Фоновый сервис: обновление данных ЖК с homsters.kz (застройщик, цена,
диапазон площади/комнат, район) — раз в 5 дней (± джиттер). Один прогон
уже собирает всё нужное с покрытых URL, дальше это периодическое
дополнение/обновление.

Логирует в homsters.log (см. krisha-homsters.service).
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
log = logging.getLogger("homsters_service")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")
MIN_INTERVAL_H = 5 * 24 - 6
MAX_INTERVAL_H = 5 * 24 + 6


async def run_cycle():
    from homsters_import import fetch_all, save_to_db

    log.info("=== Homsters cycle start ===")
    try:
        found = await fetch_all()
        if not found:
            log.warning("Homsters cycle: ничего не собрано (возможно, разметка изменилась)")
            return
        await save_to_db(found)
        log.info("=== Homsters cycle done: %d ЖК обработано ===", len(found))
    except Exception as e:
        log.error("Homsters cycle failed: %s", e, exc_info=True)

    # Застройщики: карточки + привязка ЖК к developers (тот же 5-дневный цикл)
    try:
        from homsters_developers_import import fetch_developers, save_to_db as save_devs
        devs = await fetch_developers()
        if devs:
            await save_devs(devs)
            log.info("=== Developers import done: %d застройщиков ===", len(devs))
    except Exception as e:
        log.error("Developers import failed: %s", e, exc_info=True)


async def main():
    from bot.db.pg import init_pool

    await init_pool(DATABASE_URL)
    log.info("=== Homsters service started ===")

    while True:
        try:
            await run_cycle()
        except Exception as e:
            log.error("Homsters loop error: %s", e, exc_info=True)

        sleep_h = random.uniform(MIN_INTERVAL_H, MAX_INTERVAL_H)
        log.info("Sleeping %.1f hours (~%.1f days)...\n", sleep_h, sleep_h / 24)
        await asyncio.sleep(sleep_h * 3600)


if __name__ == "__main__":
    asyncio.run(main())
