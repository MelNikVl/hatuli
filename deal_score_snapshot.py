#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ежедневный снимок Deal Score (Фаза A.5, п.2-3 вердикт-стратегии, задача
2026-08-14, см. docs/verdict_strategy.md) — deal_score_snapshots.

НЕ ПЕРЕСЧИТЫВАЕТ ФОРМУЛУ. Читает ТЕКУЩЕЕ уже посчитанное состояние
apartment_listings (score_total/hex_details/deal_confidence/bargain_*) —
то, что apply_deal_scores() (bot/core/deal_score.py) насчитал в своём
собственном цикле парсера — и просто фиксирует его с меткой времени +
версией формулы + git-коммитом. Если бы этот скрипт сам пересчитывал
Deal Score задним числом, снимок перестал бы быть честным историческим
фактом "что показывалось пользователю в этот день" — стал бы ретроактивной
переоценкой прошлого сегодняшним кодом.

Скоуп: только is_active IS NOT FALSE (архивные не пересчитываются
apply_deal_scores() и не меняются — их hex_details уже заморожен с
последнего дня активности, повторный ежедневный снимок неизменного
значения не добавляет временной информации, только раздувает таблицу).

score_version — hex_details.version (bot/core/deal_score.compute_deal_
scores() уже кладёт "version": 4 в каждую запись, задача просто читает
существующее поле). git_commit — bot.git_info.git_hash() (тот же
механизм, что service_apartments.py/service_web.py пишут в лог при
старте).

inputs_hash — md5 от price/area/rooms/floor/floors_total/complex_name/
resolved_house_id/finish_level/is_active НА МОМЕНТ снимка: позволяет при
последующем анализе истории отличить "скор пересчитан, потому что
вводные объявления изменились" от "скор просто давно не пересчитывался,
вводные те же" — без пересчёта самой формулы.

data_completeness — на эту дату математически равен deal_confidence
(мирроринг, задача Фазы A.5 не вводит отдельную метрику полноты) —
колонка существует заранее под именем, к которому переход планируется
после калибровки (Фаза A п.5/C, см. verdict_strategy.md §4), чтобы
исторический ряд уже писался под правильным именем.

Расписание: krisha-deal-score-snapshot.timer (после listing-snapshot,
08:35 ежедневно).
Разовая проверка: venv/bin/python deal_score_snapshot.py
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
    handlers=[logging.FileHandler("deal_score_snapshot.log", encoding="utf-8", errors="replace")],
)
log = logging.getLogger("deal_score_snapshot")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

SNAPSHOT_SQL = """
    INSERT INTO deal_score_snapshots (
        listing_id, observed_at, score_version, git_commit,
        score_total, price_score, quality_score, market_score, risk_score,
        risk_flags, deal_confidence, data_completeness,
        bargain_target_price, bargain_discount_pct, bargain_analogs_count, bargain_method,
        inputs_hash
    )
    SELECT
        id, now(), (hex_details::jsonb->>'version')::int, $1,
        score_total,
        (hex_details::jsonb->'components'->'price'->>'score')::int,
        (hex_details::jsonb->'components'->'quality'->>'score')::int,
        (hex_details::jsonb->'components'->'market'->>'score')::int,
        (hex_details::jsonb->'components'->'risk'->>'score')::int,
        hex_details::jsonb->'flags',
        deal_confidence, deal_confidence,
        bargain_target, bargain_discount_pct, comparables_cnt, bargain_method,
        md5(concat_ws('|',
            price::text, area::text, rooms::text, floor::text, floors_total::text,
            complex_name, resolved_house_id::text, finish_level, is_active::text))
    FROM apartment_listings
    WHERE is_active IS NOT FALSE AND hex_details IS NOT NULL
      {listing_filter}
"""


async def run_snapshot(listing_ids: list[str] | None = None) -> int:
    """listing_ids — опциональный фильтр (по умолчанию None: вся активная
    база, как в проде по таймеру). Нужен для тестов — без него каждый
    прогон теста снимал бы снимок ВСЕЙ базы (десятки тысяч строк) вместо
    одной синтетической записи, раздувая deal_score_snapshots на каждый
    pytest-запуск."""
    from bot.db.pg import execute, fetchval
    from bot.git_info import git_hash

    commit = git_hash()
    if listing_ids is not None:
        sql = SNAPSHOT_SQL.format(listing_filter="AND id = ANY($2::text[])")
        status = await execute(sql, commit, listing_ids)
    else:
        sql = SNAPSHOT_SQL.format(listing_filter="")
        status = await execute(sql, commit)
    n = int(status.rsplit(" ", 1)[-1]) if status else 0
    total_today = await fetchval(
        "SELECT COUNT(*) FROM deal_score_snapshots WHERE observed_at::date = CURRENT_DATE")
    log.info("deal_score_snapshot: %d строк вставлено (git=%s), всего снимков за сегодня %d",
              n, commit, total_today)
    return n


async def main() -> None:
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    try:
        await run_snapshot()
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
