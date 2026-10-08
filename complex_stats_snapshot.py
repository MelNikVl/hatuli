#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ежедневный снимок статистики ЖК (Г3, задача 2026-08-14, см.
docs/data_collection_audit.md) — complexes.avg_price_m2/.avg_yield/
.listings_count перезаписываются каждый цикл парсера БЕЗ истории;
complex_stats_history даёт снимок "на дату" для будущих графиков
динамики цены по ЖК.

Один INSERT...SELECT на всю базу разом (не цикл по ЖК) — считает
avg_price_m2/avg_yield/listings_count заново из apartment_listings.
Приоритет: complex_id, иначе resolved_house_id, иначе однозначное имя.
canonical_id объединяет aliases; дом под зонтиком остаётся отдельным ЖК.

avg_yield считается из apartment_listings.yield_pct напрямую (НЕ из
complexes.avg_yield — та колонка существует в схеме, но не имеет ни
одного живого писателя, всегда NULL).

UNIQUE(complex_id, date) — повторный запуск в тот же день идемпотентен
(ON CONFLICT DO UPDATE), можно смело ретраить вручную.

**avg_dom_days/price_drop_share_30d/price_drop_share_60d** (Фаза L1
продуктового трека «Локация», docs/location_product_design.md §7,
задача 2026-08-14, миграция 072) — тот же писатель, тот же
listing_complex CTE, не второй скрипт:

  avg_dom_days — среди АКТИВНЫХ объявлений комплекса, средний возраст
  (now() - first_seen) в днях. Это НЕ время до продажи (даты продажи в
  системе нет вовсе, см. docs/verdict_strategy.md §3.5 — то же
  ограничение, что уже отмечено для disappeared_within_30d) — прокси
  "сколько живут текущие объявления", не настоящий DOM.

  price_drop_share_30d/60d — доля АКТИВНЫХ объявлений комплекса, у
  которых есть ХОТЯ БЫ ОДНА запись price_history со снижением цены
  (new_price < old_price) за последние 30/60 дней. Отсутствие записей
  в price_history для объявления не считается "снижения не было в
  выборке" молча пропущенным — оно явно попадает в знаменатель со
  значением 0 (не NULL), потому что "не снижалась" — валидный,
  измеренный факт для объявления с известной историей цены, а не Unknown.

  Тренд (дельта к предыдущему периоду) НЕ считается и не хранится
  здесь — читается на запросе как разница двух дневных снимков (см.
  migrations/072 докстринг).

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
    -- На объявление ровно одна строка: явный complex_id авторитетен,
    -- legacy house/name используются только до его назначения. Алиасы
    -- нормализуются по canonical_id, родитель дома НЕ заменяет его id.
    WITH cx AS (
        SELECT id, COALESCE(canonical_id, id) AS cid, complex_name_key(name) AS n
        FROM complexes
        WHERE COALESCE(is_garbage, FALSE) = FALSE
          AND COALESCE(is_street, FALSE) = FALSE
          AND COALESCE(canonical_reason, '') <> 'junk_unmatched'
    ), names AS (
        SELECT n, cid FROM cx
        UNION ALL
        SELECT ca.normalized_name, cx.cid FROM complex_aliases ca JOIN cx ON cx.id = ca.complex_id
        WHERE ca.status = 'verified'
    ), name_keys AS (
        SELECT n, min(cid) AS cid
        FROM names
        WHERE n <> ''
        GROUP BY n
        HAVING count(DISTINCT cid) = 1
    ), listing_complex AS (
        SELECT al.id AS listing_id, al.price, al.area, al.yield_pct, al.is_active, al.first_seen,
               COALESCE(bound.cid, house.cid, byname.cid) AS complex_id
        FROM apartment_listings al
        LEFT JOIN cx bound ON bound.id = al.complex_id
        LEFT JOIN cx house
          ON al.complex_id IS NULL AND al.complex_resolution IS DISTINCT FROM 'unbound'
         AND house.id = al.resolved_house_id
        LEFT JOIN name_keys byname
          ON al.complex_id IS NULL AND al.complex_resolution IS DISTINCT FROM 'unbound'
         AND al.resolved_house_id IS NULL
         AND byname.n = complex_name_key(al.complex_name)
        WHERE COALESCE(al.is_duplicate, FALSE) = FALSE
    ),
    -- Фаза L1 (миграция 072): у кого из listing_complex было снижение
    -- цены (new_price < old_price) в price_history за последние 30/60
    -- дней — per-listing флаг, агрегируется в долю ниже. bool_or, не
    -- COUNT — один listing_id может иметь несколько строк price_history,
    -- достаточно ХОТЯ БЫ ОДНОГО снижения в окне.
    price_drops AS (
        SELECT listing_id,
               bool_or(changed_at >= now() - INTERVAL '30 days') AS dropped_30d,
               bool_or(changed_at >= now() - INTERVAL '60 days') AS dropped_60d
        FROM price_history
        WHERE new_price < old_price
        GROUP BY listing_id
    )
    INSERT INTO complex_stats_history (
        complex_id, date, avg_price_m2, avg_yield, listings_count,
        avg_dom_days, price_drop_share_30d, price_drop_share_60d
    )
    SELECT lc.complex_id, CURRENT_DATE,
           AVG(lc.price / NULLIF(lc.area, 0)) FILTER (WHERE lc.is_active IS NOT FALSE) AS avg_price_m2,
           AVG(lc.yield_pct) FILTER (WHERE lc.is_active IS NOT FALSE AND lc.yield_pct > 0) AS avg_yield,
           COUNT(*) FILTER (WHERE lc.is_active IS NOT FALSE) AS listings_count,
           AVG(EXTRACT(EPOCH FROM (now() - lc.first_seen)) / 86400.0)
               FILTER (WHERE lc.is_active IS NOT FALSE) AS avg_dom_days,
           AVG(CASE WHEN pd.dropped_30d THEN 1.0 ELSE 0.0 END)
               FILTER (WHERE lc.is_active IS NOT FALSE) AS price_drop_share_30d,
           AVG(CASE WHEN pd.dropped_60d THEN 1.0 ELSE 0.0 END)
               FILTER (WHERE lc.is_active IS NOT FALSE) AS price_drop_share_60d
    FROM listing_complex lc
    LEFT JOIN price_drops pd ON pd.listing_id = lc.listing_id
    WHERE lc.complex_id IS NOT NULL
    GROUP BY lc.complex_id
    HAVING COUNT(*) FILTER (WHERE lc.is_active IS NOT FALSE) > 0
    ON CONFLICT (complex_id, date) DO UPDATE SET
        avg_price_m2 = EXCLUDED.avg_price_m2,
        avg_yield = EXCLUDED.avg_yield,
        listings_count = EXCLUDED.listings_count,
        avg_dom_days = EXCLUDED.avg_dom_days,
        price_drop_share_30d = EXCLUDED.price_drop_share_30d,
        price_drop_share_60d = EXCLUDED.price_drop_share_60d,
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
