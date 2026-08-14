#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ежедневный снимок объявлений (Фаза A, п.1 вердикт-стратегии, задача
2026-08-14, см. docs/verdict_strategy.md §5) — listing_snapshots даёт
равномерную дневную сетку (listing_id, price, views, is_active) для
будущего расчёта outcome-меток (Фаза A, п.2: disappeared_within_30d,
survives_90d, time_on_market, views_velocity).

Один INSERT...SELECT на всю базу разом (не цикл по объявлению) — снимает
ВСЕ объявления (активные и архивные — is_active сам по себе значимая
часть снимка, архивные не фильтруются, иначе "исчезло" не с чем будет
сравнить).

UNIQUE(listing_id, date) — повторный запуск в тот же день идемпотентен
(ON CONFLICT DO UPDATE), можно смело ретраить вручную.

БЭКФИЛА НЕТ — снимков до даты первого запуска физически не существует
(см. докстринг migrations/064_listing_snapshots.sql). Таблица начинает
накапливаться строго с сегодняшнего дня.

Расписание: krisha-listing-snapshot.timer (ежедневно).
Разовая проверка: venv/bin/python listing_snapshot.py
"""
from __future__ import annotations

import asyncio
import logging
import os

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.FileHandler("listing_snapshot.log", encoding="utf-8", errors="replace")],
)
log = logging.getLogger("listing_snapshot")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

SNAPSHOT_SQL = """
    INSERT INTO listing_snapshots (listing_id, date, price, views, is_active)
    SELECT id, CURRENT_DATE, price, views_count, is_active
    FROM apartment_listings
    ON CONFLICT (listing_id, date) DO UPDATE SET
        price = EXCLUDED.price,
        views = EXCLUDED.views,
        is_active = EXCLUDED.is_active,
        observed_at = now()
"""


async def run_snapshot() -> int:
    """Возвращает число объявлений, попавших в сегодняшний снимок."""
    from bot.db.pg import execute, fetchval
    status = await execute(SNAPSHOT_SQL)
    # asyncpg возвращает "INSERT 0 N" — N затронутых строк.
    n = int(status.rsplit(" ", 1)[-1]) if status else 0
    total = await fetchval(
        "SELECT COUNT(*) FROM listing_snapshots WHERE date = CURRENT_DATE")
    active = await fetchval(
        "SELECT COUNT(*) FROM listing_snapshots WHERE date = CURRENT_DATE AND is_active IS NOT FALSE")
    log.info("listing_snapshot: %d объявлений в снимке за сегодня (из них %d активных, upsert затронул %d строк)",
              total, active, n)
    return total


async def main() -> None:
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    try:
        await run_snapshot()
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
