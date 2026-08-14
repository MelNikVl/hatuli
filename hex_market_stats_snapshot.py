#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ежедневный снимок плотности предложения по гексагону (Фаза L1
продуктового трека «Локация», docs/location_product_design.md §7,
задача 2026-08-14, миграция 072) — "перенасыщение предложением" это
свойство района/гексагона, не одного ЖК (в отличие от avg_dom_days/
price_drop_share_30d/60d, которые расширили complex_stats_snapshot.py
как complex_id-агрегат, см. соседний коммит).

Гексагон не выразим в чистом SQL (та же математика bot/core/hexgrid.py::
hex_id(), что уже использует deal_score.py/bargain.py) — группировка
делается в Python, не GROUP BY на стороне БД.

resolved_house_id НЕ участвует здесь намеренно (в отличие от complex_id-
агрегатов) — это агрегация по координате самого объявления, не по
имени ЖК; зонтик/дом ни при чём, у объявления одна пара lat/lon
независимо от того, к какому ЖК резолвится его complex_name.

edge_m фиксируется НА ДАТУ СНИМКА (читается из app_settings.HEX_EDGE_M
один раз за прогон, не за каждый listing) — если ребро сетки сменится
позже, старые снимки не станут молча несопоставимы под тем же hex_id
(см. migrations/072 докстринг).

UNIQUE(hex_id, date) — повторный запуск в тот же день идемпотентен
(ON CONFLICT DO UPDATE), тот же паттерн, что complex_stats_snapshot.py.

Расписание: krisha-hex-market-stats.timer (ежедневно, сразу после
krisha-complex-stats.timer).
Разовая проверка: venv/bin/python hex_market_stats_snapshot.py
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
    handlers=[logging.FileHandler("hex_market_stats_snapshot.log", encoding="utf-8", errors="replace")],
)
log = logging.getLogger("hex_market_stats_snapshot")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")


async def run_snapshot(listing_ids: list[str] | None = None) -> int:
    """Возвращает число гексагонов в сегодняшнем снимке.

    listing_ids — опциональный скоуп ТОЛЬКО для дешёвых тестов (тот же
    паттерн, что уже есть у deal_score_snapshot.py::run_snapshot()) — не
    для прода: прод-путь (None) всегда считает по всей активной базе,
    иначе снимок дня был бы неполным."""
    from bot.db.pg import fetch, execute
    from bot.db import settings as app_settings
    from bot.core.hexgrid import hex_id as compute_hex_id

    edge_m = float(app_settings.get_int("HEX_EDGE_M", 50))

    if listing_ids is not None:
        rows = await fetch("""
            SELECT lat, lon, price, area
            FROM apartment_listings
            WHERE is_active IS NOT FALSE AND COALESCE(is_duplicate, FALSE) = FALSE
              AND lat IS NOT NULL AND lon IS NOT NULL AND id = ANY($1::text[])
        """, listing_ids)
    else:
        rows = await fetch("""
            SELECT lat, lon, price, area
            FROM apartment_listings
            WHERE is_active IS NOT FALSE AND COALESCE(is_duplicate, FALSE) = FALSE
              AND lat IS NOT NULL AND lon IS NOT NULL
        """)

    buckets: dict[str, dict] = {}
    for r in rows:
        hid = compute_hex_id(float(r["lat"]), float(r["lon"]), edge_m)
        b = buckets.setdefault(hid, {"count": 0, "price_m2_sum": 0.0, "price_m2_n": 0})
        b["count"] += 1
        if r["price"] and r["area"] and r["area"] > 0:
            b["price_m2_sum"] += r["price"] / r["area"]
            b["price_m2_n"] += 1

    for hid, b in buckets.items():
        avg_price_m2 = (b["price_m2_sum"] / b["price_m2_n"]) if b["price_m2_n"] else None
        await execute("""
            INSERT INTO hex_market_stats (hex_id, date, edge_m, listings_count, avg_price_m2)
            VALUES ($1, CURRENT_DATE, $2, $3, $4)
            ON CONFLICT (hex_id, date) DO UPDATE SET
                edge_m = EXCLUDED.edge_m,
                listings_count = EXCLUDED.listings_count,
                avg_price_m2 = EXCLUDED.avg_price_m2,
                computed_at = now()
        """, hid, edge_m, b["count"], avg_price_m2)

    log.info("hex_market_stats_snapshot: %d гексагонов в снимке за сегодня (edge_m=%.0f, %d объявлений всего)",
              len(buckets), edge_m, len(rows))
    return len(buckets)


async def main() -> None:
    from bot.db.pg import init_pool, close_pool
    from bot.db import settings as app_settings
    await init_pool(DATABASE_URL)
    try:
        # Обязательно (bot/db/settings.py докстринг: "каждый сервис должен
        # вызывать load() в начале цикла") — иначе HEX_EDGE_M читался бы
        # из пустого in-process кеша -> тихий фолбэк на дефолт 50, вместо
        # реально настроенного в app_settings значения (сейчас 100 на
        # проде, не 50 — живая находка при разработке этого коммита).
        await app_settings.load()
        await run_snapshot()
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
