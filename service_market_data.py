"""
Фоновый сервис: рыночные данные (базовая ставка НБРК, депозитные ставки
KDIF, ставки Отбасы, индекс цен на жильё stat.gov.kz) — раз в ~7 дней.
Эти показатели меняются редко (решения по ставке НБРК ~8 раз в год,
статистика — раз в месяц), чаще дёргать нет смысла.

Результаты — справочные значения в app_settings, показываются на
/admin/info. Ползунки скоринга автоматически НЕ меняются.

Разовая проверка (ничего не пишет в БД):
    venv/bin/python service_market_data.py --test

Требования: venv/bin/pip install openpyxl   (для xlsx статистики)

Логирует в market.log (см. krisha-market.service).
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
log = logging.getLogger("market_service")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")
MIN_INTERVAL_H = 7 * 24 - 12
MAX_INTERVAL_H = 7 * 24 + 12


def _print_results(results: dict) -> None:
    log.info("НБРК базовая ставка: %s%%", results.get("NBRK_BASE_RATE") or "не найдена")
    kdif = results.get("KDIF_RATES_RAW") or []
    log.info("KDIF: найдено %d ставок", len(kdif))
    for r in kdif[:8]:
        log.info("  KDIF: %s", r)
    otb = results.get("OTBASY_RATES_RAW") or []
    log.info("Отбасы: найдено %d ставок (эксперимент)", len(otb))
    for r in otb[:6]:
        log.info("  Отбасы: %s", r)
    stat = results.get("STAT_HOUSING_ASTANA_RAW") or []
    log.info("stat.gov.kz: %d строк с «Астана» (файл: %s)",
             len(stat), results.get("STAT_HOUSING_FILE_URL"))
    for r in stat[:10]:
        log.info("  stat: %s", r)


async def run_once(write: bool) -> None:
    from bot.core.market_data import update_all
    results = await update_all(write=write)
    _print_results(results)


async def main():
    test_mode = "--test" in sys.argv

    from bot.db.pg import init_pool
    await init_pool(DATABASE_URL)

    if test_mode:
        log.info("=== Market data --test (в БД НЕ пишем) ===")
        await run_once(write=False)
        log.info("--test завершён. Если данные выглядят правильно — включай "
                 "сервис krisha-market, он будет писать их в app_settings.")
        return

    log.info("=== Market data service started ===")
    while True:
        try:
            log.info("=== Market data cycle start ===")
            await run_once(write=True)
            log.info("=== Market data cycle done ===")
        except Exception as e:
            log.error("Market cycle failed: %s", e, exc_info=True)

        sleep_h = random.uniform(MIN_INTERVAL_H, MAX_INTERVAL_H)
        log.info("Sleeping %.1f hours (~%.1f days)...\n", sleep_h, sleep_h / 24)
        await asyncio.sleep(sleep_h * 3600)


if __name__ == "__main__":
    asyncio.run(main())
