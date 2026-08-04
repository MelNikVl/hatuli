"""
Фоновый сервис: обновление данных ЖК с korter.kz (класс жилья, застройщик,
район, цена/м²) — раз в сутки (±2 ч джиттер, чтобы не бить ровно по
расписанию). Один прогон ~40-60 сек (9 запросов с паузами), полный обход
занимает меньше минуты — ежедневная частота безопасна.

Длительность каждого прогона и счётчики изменений пишутся в source_runs,
сами изменения — в source_changes (см. bot/core/site_enrichment.py).
Вкладки Korter/Homsters на /admin/parsers показывают их.

Логирует в korter.log (см. krisha-korter.service).
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
log = logging.getLogger("korter_service")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:***@localhost/krisha_bot")
# Интервал парсинга — из parse_settings (ключ korter_interval_h, часы),
# по умолчанию 120 ч = 5 дней (±2 ч джиттер). Настраивается на вкладке
# Korter страницы /admin/parsers.
DEFAULT_INTERVAL_H = 120


async def _interval_h() -> float:
    try:
        from bot.db.pg import fetchval
        v = await fetchval(
            "SELECT value FROM parse_settings WHERE key = 'korter_interval_h'")
        if v:
            return max(1.0, float(v))
    except Exception as e:
        log.warning("interval read failed: %s", e)
    return DEFAULT_INTERVAL_H


async def run_cycle():
    from korter_import import fetch_all, save_to_db
    from bot.core.site_enrichment import record_run

    t0 = time.monotonic()
    log.info("=== Korter cycle start ===")
    try:
        found = await fetch_all(test=False)
        if not found:
            log.warning("Korter cycle: ничего не собрано (возможно, разметка изменилась)")
            return
        stats = await save_to_db(found)
        duration_s = round(time.monotonic() - t0, 1)
        log.info("=== Korter cycle done: %d ЖК обработано за %ss "
                 "(создано %d, изменений %d) ===",
                 len(found), duration_s, stats.get("created", 0), stats.get("changed", 0))
        await record_run("korter", datetime.now(timezone.utc), duration_s,
                         stats.get("matched", 0), stats.get("created", 0),
                         stats.get("changed", 0))
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

        interval = await _interval_h()
        sleep_h = random.uniform(max(1.0, interval - 2), interval + 2)
        log.info("Sleeping %.1f hours (~%.1f days)...\n", sleep_h, sleep_h / 24)
        await asyncio.sleep(sleep_h * 3600)


if __name__ == "__main__":
    asyncio.run(main())
