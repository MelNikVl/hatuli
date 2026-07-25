"""
Разовый догоняющий прогон: докачать координаты/ЖК для ВСЕГО текущего
бэклога объявлений без них (не только маленький батч раз в час, как в
service_apartments.py).

Тот же безопасный интервал между запросами к krisha.kz (8-15с/объявление,
см. bot/core/coord_backfill.py) — просто без часового цикла и без лимита
на батч за раз. При ~10 тыс. объявлений это займёт ЧАСЫ (десятки часов) —
осознанно медленно, чтобы не словить бан на основном источнике данных.

Запуск (в фоне, переживает разрыв SSH-сессии):
    cd /home/nik/krisha_bot
    nohup venv/bin/python backfill_coords_bulk.py >> backfill_bulk.log 2>&1 &

Прогресс — в backfill_bulk.log. Прервать можно в любой момент (Ctrl+C /
kill) — идемпотентно, при повторном запуске продолжит с необработанных
(coord_fetch_attempted_at решает, что уже пробовали недавно).
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

sys.path.insert(0, ".")

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("backfill_bulk")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")


async def main() -> None:
    from bot.db.pg import init_pool, fetchval
    from bot.core.coord_backfill import backfill_coords_and_complex
    from bot.db import settings as app_settings

    await init_pool(DATABASE_URL)
    await app_settings.load()

    total = await fetchval("""
        SELECT COUNT(*) FROM apartment_listings
        WHERE (lat IS NULL OR complex_name IS NULL OR btrim(complex_name) = ''
               OR (COALESCE(is_owner, FALSE) = FALSE AND (seller_name IS NULL OR btrim(seller_name) = ''))
               OR photos IS NULL OR photos::text IN ('[]', 'null')
               OR description IS NULL OR btrim(description) = '')
          AND is_active IS NOT FALSE AND url IS NOT NULL
    """) or 0
    log.info("=== Bulk backfill start: в очереди ~%d объявлений, ~%.1f ч при 8-15с/шт ===",
              total, total * 11.5 / 3600)

    # На время bulk-прогона отключаем инлайн-бэкфилл в часовом цикле
    # service_apartments.py — иначе два процесса будут одновременно
    # дёргать krisha.kz детальными запросами. Восстанавливаем в finally.
    prev_batch = app_settings.get_int("COORD_BACKFILL_BATCH", 80)
    await app_settings.set("COORD_BACKFILL_BATCH", "0")
    log.info("COORD_BACKFILL_BATCH временно = 0 (был %d) на время bulk-прогона", prev_batch)

    try:
        # min_age_days=0: игнорируем троидневный кулдаун — это разовый прогон
        # по текущему бэклогу, а не часть регулярного цикла.
        res = await backfill_coords_and_complex(limit=total or 1, min_age_days=0)
        log.info("=== Bulk backfill done: обработано %d, координаты %d, ЖК %d ===",
                  res["attempted"], res["got_coords"], res["got_complex"])
    finally:
        await app_settings.set("COORD_BACKFILL_BATCH", str(prev_batch))
        log.info("COORD_BACKFILL_BATCH восстановлен = %d", prev_batch)


if __name__ == "__main__":
    asyncio.run(main())
