#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Outcome-метки (Фаза A п.2 + Фаза A.5 п.4 вердикт-стратегии, задача
2026-08-14, см. docs/verdict_strategy.md) — disappeared_within_30d/
price_reduction_within_30d/survives_90d/time_on_market/views_velocity
(Фаза A) + clean_disappearance_within_30d/relisted_within_60d/
possibly_relisted/possibly_moderation_removed/observation_days/censored/
outcome_notes (Фаза A.5 — уточнение, НЕ замена: disappeared_within_30d
сам по себе — прокси ЛИКВИДНОСТИ, не продажи, см. verdict_strategy.md,
Фаза A.5 п.7).

**Известное временное ограничение на дату задачи (2026-08-14), честно
зафиксировано, не баг**: clean_disappearance_within_30d/relisted_within_
60d разрешаются в TRUE/FALSE только после закрытия 60-дневного
релист-окна (or раньше — если найден строгий кандидат, см. ниже).
price_history (граница честности disappeared_within_30d) стартовала
2026-07-09 — на дату задачи ей ~36 дней, МЕНЬШЕ 60. Значит "disappeared_
within_30d=TRUE" (нужен свежий first_seen, после границы) и "окно
релиста закрыто" (нужен archived_at минимум 60 дней назад) математически
не могут выполниться одновременно ДО ~2026-09-08 — до этой даты
clean_disappearance_within_30d будет TRUE только в единичных случаях
(строгий релист-кандидат найден раньше закрытия окна — сразу FALSE) либо
чаще всего NULL, `relisted_resolved` тоже будет заметно меньше total
архивных. Это ожидаемо и пройдёт само по мере накопления истории — не
искать здесь баг повторно.

Один SQL UPSERT на всю базу разом (не цикл по объявлению), тот же
паттерн, что complex_stats_snapshot.py/listing_snapshot.py. Запускается
и как разовый бэкфил (вся история price_history/archived_at — годы
данных), и как ongoing-пересчёт по таймеру: NULL-метки со временем
разрешаются (окно 30/60/90 дней закрывается), поэтому пересчёт — не
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

survives_90d/time_on_market/observation_days/censored НЕ имеют такого
ограничения — archived_at/first_seen прослеживаются на всю историю
apartment_listings.

views_velocity ограничена views_history (Г2, живёт только с 2026-08-14).

**Релист-эвристика** (relisted_within_60d/possibly_relisted): на Krisha
"поднятие" объявления часто создаёт НОВЫЙ listing_id для того же
физического объекта — без этой проверки "быстрый архив без снижений"
неотличим от "продавец просто переопубликовал то же самое". Два порога
уверенности (тот же принцип auto/review, что bot/core/entity_resolution.
py score_match()): relisted_within_60d — строгое совпадение (ЖК+комнаты+
этаж+площадь±5%+цена±15%, новое объявление в течение 60 дней после
архивации), possibly_relisted — ослабленное (ЖК+комнаты+площадь±10%, без
этажа/цены), НЕ пойманное строгим правилом. Возможен ложноотрицательный
результат (продавец сменил текст/фото/чуть иную площадь) — эвристика, не
факт, отсюда "possibly" и говорящее imя.

**possibly_moderation_removed** — снято раньше, чем система хоть раз
подтянула детальную страницу (archived_at-first_seen<=2 дня И
details_fetched НЕ TRUE) — слабый сигнал, явно так и подписан.

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
    ),
    relist_candidates AS (
        -- Плоская проекция всех объявлений с нормализованным именем ЖК —
        -- джойнится ХЭШЕМ по (cx_norm, rooms) ниже, а не коррелированным
        -- подзапросом на каждую архивную строку (тот же класс бага, что
        -- уже был в deal_score.py apply_deal_scores() — O(n²) full-scan
        -- по 30с command_timeout, см. комментарий в том файле).
        SELECT id, lower(trim(complex_name)) AS cx_norm, rooms, floor, area, price, first_seen
        FROM apartment_listings
        WHERE complex_name IS NOT NULL AND btrim(complex_name) != ''
          AND rooms IS NOT NULL AND area IS NOT NULL AND price IS NOT NULL
    ),
    relist_match AS (
        -- Только архивные — активные не могут быть "релистнуты" (ещё не
        -- ушли с рынка).
        SELECT al.id AS listing_id,
               BOOL_OR(
                   c.floor IS NOT DISTINCT FROM al.floor
                   AND c.area BETWEEN al.area * 0.95 AND al.area * 1.05
                   AND c.price BETWEEN al.price * 0.85 AND al.price * 1.15
               ) AS strict_match,
               BOOL_OR(
                   c.area BETWEEN al.area * 0.90 AND al.area * 1.10
               ) AS loose_match
        FROM apartment_listings al
        JOIN relist_candidates c
          ON c.cx_norm = lower(trim(al.complex_name))
         AND c.rooms = al.rooms
         AND c.id != al.id
         AND c.first_seen > al.archived_at
         AND c.first_seen <= al.archived_at + INTERVAL '60 days'
        WHERE al.archived_at IS NOT NULL
          AND al.complex_name IS NOT NULL AND btrim(al.complex_name) != ''
          AND al.area IS NOT NULL AND al.price IS NOT NULL AND al.rooms IS NOT NULL
        GROUP BY al.id
    )
    INSERT INTO outcome_labels
        (listing_id, disappeared_within_30d, price_reduction_within_30d,
         survives_90d, time_on_market, views_velocity,
         clean_disappearance_within_30d, relisted_within_60d, possibly_relisted,
         possibly_moderation_removed, observation_days, censored, outcome_notes,
         computed_at)
    SELECT
        al.id,
        d30.disappeared_within_30d,
        d30.price_reduction_within_30d,
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
        -- clean_disappearance_within_30d — Фаза A.5: disappeared_within_30d
        -- за вычетом вероятного релиста/модерации (см. докстринг модуля).
        -- window_closed считается ЯВНО (al.archived_at+60д <= now()), а не
        -- выводится из "rm пуст" — relist_match CTE использует INNER JOIN
        -- (строки только там, где кандидат реально нашёлся), поэтому
        -- "rm.strict_match IS NULL" сам по себе означает и "кандидатов
        -- нет, но окно ещё открыто" И "кандидатов нет, окно закрыто" —
        -- эти два случая нельзя было различить без отдельного флага (это
        -- и была ошибка первой версии запроса, поймана тестом).
        CASE
            WHEN d30.disappeared_within_30d IS NULL THEN NULL
            WHEN d30.disappeared_within_30d IS FALSE THEN FALSE
            WHEN modrem.possibly_moderation_removed THEN FALSE
            WHEN COALESCE(rm.strict_match, FALSE) THEN FALSE
            WHEN now() < al.archived_at + INTERVAL '60 days' THEN NULL
            ELSE TRUE
        END AS clean_disappearance_within_30d,
        CASE
            WHEN al.archived_at IS NULL THEN NULL
            WHEN COALESCE(rm.strict_match, FALSE) THEN TRUE
            WHEN now() < al.archived_at + INTERVAL '60 days' THEN NULL
            ELSE FALSE
        END AS relisted_within_60d,
        CASE
            WHEN al.archived_at IS NULL THEN NULL
            WHEN COALESCE(rm.strict_match, FALSE) THEN FALSE  -- уже в strict, "possibly" ей не нужен
            WHEN COALESCE(rm.loose_match, FALSE) THEN TRUE
            WHEN now() < al.archived_at + INTERVAL '60 days' THEN NULL
            ELSE FALSE
        END AS possibly_relisted,
        modrem.possibly_moderation_removed,
        EXTRACT(day FROM COALESCE(al.archived_at, now()) - al.first_seen)::INT AS observation_days,
        (al.archived_at IS NULL) AS censored,
        NULLIF(concat_ws('; ',
            CASE WHEN al.first_seen < b.price_hist_start
                 THEN 'price_history не покрывает начало окна' END,
            CASE WHEN modrem.possibly_moderation_removed
                 THEN 'подозрение на снятие модерацией (архив <=2дн, детали не подтянуты)' END,
            CASE WHEN COALESCE(rm.strict_match, FALSE)
                 THEN 'похоже на релист (совпадение ЖК/комнаты/этаж/площадь/цена, окно 60дн)' END,
            CASE WHEN COALESCE(rm.loose_match, FALSE) AND NOT COALESCE(rm.strict_match, FALSE)
                 THEN 'возможный релист (слабое совпадение ЖК/комнаты/площадь)' END,
            CASE WHEN al.archived_at IS NOT NULL AND now() < al.archived_at + INTERVAL '60 days'
                 THEN 'окно релиста (60дн) ещё не закрылось' END,
            CASE WHEN al.archived_at IS NULL THEN 'censored: ещё активно, исход не финален' END
        ), '') AS outcome_notes,
        now()
    FROM apartment_listings al
    CROSS JOIN bounds b
    LEFT JOIN views_span vs ON vs.listing_id = al.id
    LEFT JOIN relist_match rm ON rm.listing_id = al.id
    LEFT JOIN reductions r ON r.listing_id = al.id
    LEFT JOIN LATERAL (
        SELECT
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
            CASE
                WHEN r.reduction_in_30d THEN TRUE
                WHEN al.first_seen < b.price_hist_start THEN NULL
                WHEN al.archived_at IS NOT NULL AND al.archived_at - al.first_seen <= INTERVAL '30 days' THEN FALSE
                WHEN now() - al.first_seen >= INTERVAL '30 days' THEN FALSE
                ELSE NULL
            END AS price_reduction_within_30d
    ) d30 ON TRUE
    LEFT JOIN LATERAL (
        SELECT (al.archived_at IS NOT NULL
                AND al.archived_at - al.first_seen <= INTERVAL '2 days'
                AND al.details_fetched IS NOT TRUE) AS possibly_moderation_removed
    ) modrem ON TRUE
    WHERE ({listing_filter})
    ON CONFLICT (listing_id) DO UPDATE SET
        disappeared_within_30d = EXCLUDED.disappeared_within_30d,
        price_reduction_within_30d = EXCLUDED.price_reduction_within_30d,
        survives_90d = EXCLUDED.survives_90d,
        time_on_market = EXCLUDED.time_on_market,
        views_velocity = EXCLUDED.views_velocity,
        clean_disappearance_within_30d = EXCLUDED.clean_disappearance_within_30d,
        relisted_within_60d = EXCLUDED.relisted_within_60d,
        possibly_relisted = EXCLUDED.possibly_relisted,
        possibly_moderation_removed = EXCLUDED.possibly_moderation_removed,
        observation_days = EXCLUDED.observation_days,
        censored = EXCLUDED.censored,
        outcome_notes = EXCLUDED.outcome_notes,
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
        COUNT(views_velocity) AS velocity_known,
        COUNT(clean_disappearance_within_30d) AS clean_resolved,
        COUNT(*) FILTER (WHERE clean_disappearance_within_30d) AS clean_true,
        COUNT(relisted_within_60d) AS relisted_resolved,
        COUNT(*) FILTER (WHERE relisted_within_60d) AS relisted_true,
        COUNT(*) FILTER (WHERE possibly_relisted) AS possibly_relisted_true,
        COUNT(*) FILTER (WHERE possibly_moderation_removed) AS possibly_modrem_true,
        COUNT(*) FILTER (WHERE censored) AS censored_true,
        ROUND(AVG(observation_days)) AS observation_days_avg
    FROM outcome_labels
"""


async def run_recompute(listing_ids: list[str] | None = None) -> dict:
    """listing_ids — опциональный фильтр (по умолчанию None: вся база,
    как в проде по таймеру). Нужен для тестов — без него каждый прогон
    теста пересчитывал бы метки ВСЕХ 47К+ объявлений (~2с на прогон)
    вместо одной синтетической записи (тот же приём, что deal_score_
    snapshot.py — задача Фазы A.5 п.3)."""
    from bot.db.pg import execute, fetchrow
    if listing_ids is not None:
        sql = RECOMPUTE_SQL.format(listing_filter="al.id = ANY($1::text[])")
        status = await execute(sql, listing_ids)
    else:
        sql = RECOMPUTE_SQL.format(listing_filter="TRUE")
        status = await execute(sql)
    n = int(status.rsplit(" ", 1)[-1]) if status else 0
    report = dict(await fetchrow(REPORT_SQL))
    log.info(
        "outcome_labels: upsert затронул %d строк. total=%s disappeared(resolved/true/false)=%s/%s/%s "
        "reduction(resolved/true)=%s/%s survives(resolved/true)=%s/%s tom(known/avg)=%s/%s velocity_known=%s "
        "clean(resolved/true)=%s/%s relisted(resolved/true)=%s/%s possibly_relisted=%s possibly_modrem=%s "
        "censored=%s observation_days_avg=%s",
        n, report["total"],
        report["disappeared_resolved"], report["disappeared_true"], report["disappeared_false"],
        report["reduction_resolved"], report["reduction_true"],
        report["survives_resolved"], report["survives_true"],
        report["tom_known"], report["tom_avg"], report["velocity_known"],
        report["clean_resolved"], report["clean_true"],
        report["relisted_resolved"], report["relisted_true"],
        report["possibly_relisted_true"], report["possibly_modrem_true"],
        report["censored_true"], report["observation_days_avg"],
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
