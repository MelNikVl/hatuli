"""
Фоновый сервис: обновление данных ЖК с homsters.kz (застройщик, цена,
диапазон площади/комнат, район) + каталог застройщиков — раз в сутки
(±2 ч джиттер). Полный обход ~45-50 мин (15 страниц ЖК + каталог и
карточки 253 застройщиков, паузы 3-5 сек), ежедневная частота щадящая.

Длительность каждого прогона и счётчики изменений пишутся в source_runs,
сами изменения — в source_changes (см. bot/core/site_enrichment.py).
Вкладки Korter/Homsters на /admin/parsers показывают их.

Логирует в homsters.log (см. krisha-homsters.service).
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, ".")

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("homsters_service")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:***@localhost/krisha_bot")
# Интервал парсинга — из parse_settings (ключ homsters_interval_h, часы),
# по умолчанию 120 ч = 5 дней (±2 ч джиттер). Настраивается на вкладке
# Homsters страницы /admin/parsers.
DEFAULT_INTERVAL_H = 120


async def _interval_h() -> float:
    try:
        from bot.db.pg import fetchval
        v = await fetchval(
            "SELECT value FROM parse_settings WHERE key = 'homsters_interval_h'")
        if v:
            return max(1.0, float(v))
    except Exception as e:
        log.warning("interval read failed: %s", e)
    return DEFAULT_INTERVAL_H


async def run_cycle():
    from homsters_import import fetch_all, save_to_db
    from bot.core.site_enrichment import record_run

    t0 = time.monotonic()
    stats = {}
    log.info("=== Homsters cycle start ===")
    try:
        found = await fetch_all()
        if not found:
            log.warning("Homsters cycle: ничего не собрано (возможно, разметка изменилась)")
            return
        stats = await save_to_db(found)
        log.info("=== Homsters cycle done: %d ЖК обработано ===", len(found))
    except Exception as e:
        log.error("Homsters cycle failed: %s", e, exc_info=True)

    # Застройщики: карточки + привязка ЖК к developers (тот же суточный цикл)
    try:
        from homsters_developers_import import fetch_developers, save_to_db as save_devs
        devs = await fetch_developers()
        if devs:
            await save_devs(devs)
            log.info("=== Developers import done: %d застройщиков ===", len(devs))
    except Exception as e:
        log.error("Developers import failed: %s", e, exc_info=True)

    duration_s = round(time.monotonic() - t0, 1)
    log.info("=== Homsters полный обход занял %ss ===", duration_s)
    try:
        await record_run("homsters", datetime.now(timezone.utc), duration_s,
                         stats.get("matched", 0), stats.get("created", 0),
                         stats.get("changed", 0))
    except Exception as e:
        log.error("record_run failed: %s", e)


async def main():
    from bot.db.pg import init_pool

    await init_pool(DATABASE_URL)
    log.info("=== Homsters service started ===")

    while True:
        try:
            await run_cycle()
        except Exception as e:
            log.error("Homsters loop error: %s", e, exc_info=True)

        interval = await _interval_h()
        sleep_h = random.uniform(max(1.0, interval - 2), interval + 2)
        log.info("Sleeping %.1f hours (~%.1f days)...\n", sleep_h, sleep_h / 24)
        await asyncio.sleep(sleep_h * 3600)


if __name__ == "__main__":
    asyncio.run(main())
