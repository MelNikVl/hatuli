#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Outcome-метки (Фаза A, п.2 вердикт-стратегии, задача 2026-08-14, см.
docs/verdict_strategy.md §5) — disappeared_within_30d/
price_reduction_within_30d/survives_90d/time_on_market/views_velocity.

Один SQL UPSERT на всю базу разом (не цикл по объявлению), тот же
паттерн, что complex_stats_snapshot.py/listing_snapshot.py. Запускается
и как разовый бэкфил (вся история price_history/archived_at — годы
данных), и как ongoing-пересчёт по таймеру: NULL-метки со временем
разрешаются (окно 30/90 дней закрывается), поэтому пересчёт — не
однократная операция.

**Честная граница покрытия** (принцип "Unknown ≠ average",
verdict_strategy.md §3.1): price_history коллектится не с самого начала
истории apartment_listings — MIN(changed_at) в price_history считается
динамически (не хардкодится датой), для объявлений с first_seen РАНЬШЕ
этой границы disappeared_within_30d/price_reduction_within_30d ставятся
NULL, если положительного события (снижения) не нашлось — отсутствие
данных не равно отсутствию события. Найденное СНИЖЕНИЕ — это позитивное
свидетельство, оно засчитывается независимо от границы покрытия (на TRUE
нужно только одно найденное событие, не полное покрытие окна).

survives_90d/time_on_market НЕ имеют такого ограничения — archived_at/
first_seen прослеживаются на всю историю apartment_listings.

views_velocity ограничена views_history (Г2, живёт только с 2026-08-14) —
на дату первого запуска этого скрипта будет NULL почти для всех
(меньше суток данных, скорости считать не из чего) — это ОЖИДАЕМО, не
баг: набирается по мере накопления views_history тем же ongoing-
пересчётом.

Расписание: krisha-outcome-labels.timer (ежедневно, после
listing-snapshot).
Разовый прогон/бэкфил: venv/bin/python outcome_labels_recompute.py
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
    handlers=[logging.FileHandler("outcome_labels_recompute.log", encoding="utf-8", errors="replace")],
)
log = logging.getLogger("outcome_labels_recompute")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

# price_hist_start — динамическая граница покрытия price_history, не
# хардкод: MIN(changed_at) на момент запуска (растягивается, если данные
# когда-нибудь будут импортированы задним числом откуда-то ещё).
RECOMPUTE_SQL = """
    WITH bounds AS (
        SELECT MIN(changed_at) AS price_hist_start FROM price_history
    ),
    reductions AS (
        -- Для каждого объявления: было ли хоть одно снижение (new<old) в
        -- первые 30 дней от first_seen (или до архивации, если раньше).
        SELECT al.id AS listing_id,
               EXISTS (
                   SELECT 1 FROM price_history ph
                   WHERE ph.listing_id = al.id
                     AND ph.new_price < ph.old_price
                     AND ph.changed_at <= al.first_seen + INTERVAL '30 days'
                     AND ph.changed_at <= COALESCE(al.archived_at, now())
               ) AS reduction_in_30d
        FROM apartment_listings al
    ),
    views_span AS (
        -- Скорость просмотров: (последнее-первое)/дни между наблюдениями,
        -- только если разброс >= 1 суток (иначе деление на почти-ноль).
        SELECT listing_id,
               MIN(observed_at) AS first_obs, MAX(observed_at) AS last_obs,
               (ARRAY_AGG(views_count ORDER BY observed_at ASC))[1] AS first_views,
               (ARRAY_AGG(views_count ORDER BY observed_at DESC))[1] AS last_views
        FROM views_history
        GROUP BY listing_id
    )
    INSERT INTO outcome_labels
        (listing_id, disappeared_within_30d, price_reduction_within_30d,
         survives_90d, time_on_market, views_velocity, computed_at)
    SELECT
        al.id,
        -- disappeared_within_30d: "быстрый архив без снижений"
        CASE
            WHEN al.archived_at IS NOT NULL AND al.archived_at - al.first_seen <= INTERVAL '30 days' THEN
                CASE
                    WHEN r.reduction_in_30d THEN FALSE
                    WHEN al.first_seen < b.price_hist_start THEN NULL
                    ELSE TRUE
                END
            WHEN al.archived_at IS NOT NULL THEN FALSE
            WHEN now() - al.first_seen > INTERVAL '30 days' THEN FALSE
            ELSE NULL
        END AS disappeared_within_30d,
        -- price_reduction_within_30d: независимая метка
        CASE
            WHEN r.reduction_in_30d THEN TRUE
            WHEN al.first_seen < b.price_hist_start THEN NULL
            WHEN al.archived_at IS NOT NULL AND al.archived_at - al.first_seen <= INTERVAL '30 days' THEN FALSE
            WHEN now() - al.first_seen >= INTERVAL '30 days' THEN FALSE
            ELSE NULL
        END AS price_reduction_within_30d,
        -- survives_90d
        CASE
            WHEN al.archived_at IS NOT NULL THEN (al.archived_at - al.first_seen >= INTERVAL '90 days')
            WHEN now() - al.first_seen >= INTERVAL '90 days' THEN TRUE
            ELSE NULL
        END AS survives_90d,
        -- time_on_market: только для разрешённых (архивных), иначе censored
        CASE WHEN al.archived_at IS NOT NULL
             THEN EXTRACT(day FROM al.archived_at - al.first_seen)::INT
             ELSE NULL END AS time_on_market,
        -- views_velocity: просмотров/день, только при разбросе >= 1 суток
        CASE WHEN vs.last_obs IS NOT NULL AND vs.last_obs - vs.first_obs >= INTERVAL '1 day'
             THEN ROUND((vs.last_views - vs.first_views) / EXTRACT(epoch FROM (vs.last_obs - vs.first_obs)) * 86400, 2)
             ELSE NULL END AS views_velocity,
        now()
    FROM apartment_listings al
    CROSS JOIN bounds b
    LEFT JOIN reductions r ON r.listing_id = al.id
    LEFT JOIN views_span vs ON vs.listing_id = al.id
    ON CONFLICT (listing_id) DO UPDATE SET
        disappeared_within_30d = EXCLUDED.disappeared_within_30d,
        price_reduction_within_30d = EXCLUDED.price_reduction_within_30d,
        survives_90d = EXCLUDED.survives_90d,
        time_on_market = EXCLUDED.time_on_market,
        views_velocity = EXCLUDED.views_velocity,
        computed_at = now()
"""

REPORT_SQL = """
    SELECT
        COUNT(*) AS total,
        COUNT(disappeared_within_30d) AS disappeared_resolved,
        COUNT(*) FILTER (WHERE disappeared_within_30d) AS disappeared_true,
        COUNT(*) FILTER (WHERE disappeared_within_30d IS FALSE) AS disappeared_false,
        COUNT(price_reduction_within_30d) AS reduction_resolved,
        COUNT(*) FILTER (WHERE price_reduction_within_30d) AS reduction_true,
        COUNT(survives_90d) AS survives_resolved,
        COUNT(*) FILTER (WHERE survives_90d) AS survives_true,
        COUNT(time_on_market) AS tom_known,
        ROUND(AVG(time_on_market)) AS tom_avg,
        COUNT(views_velocity) AS velocity_known
    FROM outcome_labels
"""


async def run_recompute() -> dict:
    from bot.db.pg import execute, fetchrow
    status = await execute(RECOMPUTE_SQL)
    n = int(status.rsplit(" ", 1)[-1]) if status else 0
    report = dict(await fetchrow(REPORT_SQL))
    log.info(
        "outcome_labels: upsert затронул %d строк. total=%s disappeared(resolved/true/false)=%s/%s/%s "
        "reduction(resolved/true)=%s/%s survives(resolved/true)=%s/%s tom(known/avg)=%s/%s velocity_known=%s",
        n, report["total"],
        report["disappeared_resolved"], report["disappeared_true"], report["disappeared_false"],
        report["reduction_resolved"], report["reduction_true"],
        report["survives_resolved"], report["survives_true"],
        report["tom_known"], report["tom_avg"], report["velocity_known"],
    )
    return report


async def main() -> None:
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    try:
        await run_recompute()
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
