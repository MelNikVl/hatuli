#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ежедневный снимок статистики ЖК (Г3, задача 2026-08-14, см.
docs/data_collection_audit.md) — complexes.avg_price_m2/.avg_yield/
.listings_count перезаписываются каждый цикл парсера БЕЗ истории;
complex_stats_history даёт снимок "на дату" для будущих графиков
динамики цены по ЖК.

Один INSERT...SELECT на всю базу разом (не цикл по ЖК) — считает
avg_price_m2/avg_yield/listings_count заново из apartment_listings,
тем же паттерном "имя ИЛИ resolved_house_id" (_listing_id_match), что
everywhere в проекте после волны 1 скоринга (House-resolution в
скоринге) — снимок ЖК-дома под зонтиком не смешивается с зонтиком.

avg_yield считается из apartment_listings.yield_pct напрямую (НЕ из
complexes.avg_yield — та колонка существует в схеме, но не имеет ни
одного живого писателя, всегда NULL).

UNIQUE(complex_id, date) — повторный запуск в тот же день идемпотентен
(ON CONFLICT DO UPDATE), можно смело ретраить вручную.

Расписание: krisha-complex-stats.timer (ежедневно).
Разовая проверка: venv/bin/python complex_stats_snapshot.py
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
    handlers=[logging.FileHandler("complex_stats_snapshot.log", encoding="utf-8", errors="replace")],
)
log = logging.getLogger("complex_stats_snapshot")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

SNAPSHOT_SQL = """
    -- listing_complex: КАЖДОЕ объявление -> РОВНО один complex_id — не
    -- OR-джойн (тот считал бы одно и то же объявление и в доме, и в
    -- зонтике разом, если resolved_house_id указывает на дом, а текст
    -- complex_name всё ещё называет зонтика: живой баг, найденный тестом
    -- этого же коммита). resolved_house_id — приоритетный путь (тот же
    -- принцип, что _listing_id_match everywhere в проекте); byname
    -- активируется, ТОЛЬКО когда resolved_house_id не задан.
    WITH listing_complex AS (
        SELECT al.id AS listing_id, al.price, al.area, al.yield_pct, al.is_active,
               COALESCE(house.id, byname.id) AS complex_id
        FROM apartment_listings al
        LEFT JOIN complexes house
          ON house.id = al.resolved_house_id AND COALESCE(house.is_garbage, FALSE) = FALSE
        LEFT JOIN complexes byname
          ON al.resolved_house_id IS NULL
         AND lower(trim(byname.name)) = lower(trim(al.complex_name))
         AND COALESCE(byname.is_garbage, FALSE) = FALSE
        WHERE COALESCE(al.is_duplicate, FALSE) = FALSE
    )
    INSERT INTO complex_stats_history (complex_id, date, avg_price_m2, avg_yield, listings_count)
    SELECT complex_id, CURRENT_DATE,
           AVG(price / NULLIF(area, 0)) FILTER (WHERE is_active IS NOT FALSE) AS avg_price_m2,
           AVG(yield_pct) FILTER (WHERE is_active IS NOT FALSE AND yield_pct > 0) AS avg_yield,
           COUNT(*) FILTER (WHERE is_active IS NOT FALSE) AS listings_count
    FROM listing_complex
    WHERE complex_id IS NOT NULL
    GROUP BY complex_id
    HAVING COUNT(*) FILTER (WHERE is_active IS NOT FALSE) > 0
    ON CONFLICT (complex_id, date) DO UPDATE SET
        avg_price_m2 = EXCLUDED.avg_price_m2,
        avg_yield = EXCLUDED.avg_yield,
        listings_count = EXCLUDED.listings_count,
        computed_at = now()
"""


async def run_snapshot() -> int:
    """Возвращает число ЖК/домов, попавших в сегодняшний снимок."""
    from bot.db.pg import execute, fetchval
    status = await execute(SNAPSHOT_SQL)
    # asyncpg возвращает "INSERT 0 N" — N затронутых строк.
    n = int(status.rsplit(" ", 1)[-1]) if status else 0
    total = await fetchval(
        "SELECT COUNT(*) FROM complex_stats_history WHERE date = CURRENT_DATE")
    log.info("complex_stats_snapshot: %d ЖК/домов в снимке за сегодня (upsert затронул %d строк)",
              total, n)
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
