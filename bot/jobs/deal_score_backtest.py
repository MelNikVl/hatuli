#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only backtest-прогон Deal Score на дату `t0` (задача 2026-08-14,
"as_of для score_total, минимальный план" — по итогам аудита временной
логики перед Фазой B, см. docs/verdict_strategy.md).

**НЕ ПИШЕТ В БД** — `apply_deal_scores(as_of=t0)` сам гарантирует
read-only путь при заданном `as_of` (UPDATE полностью пропускается, см.
докстринг той функции в bot/core/deal_score.py). Этот скрипт — первый
реальный вызыватель backtest-пути, подтверждающий, что он работает
end-to-end, изолированно от прод-таблицы (никакого риска для
`apply_deal_scores()` без параметра, который остаётся прод-путём как
раньше).

Вход: `t0` (дата/datetime) + опционально список `listing_id`. Без списка
— считает ВСЕ объявления, которые `_activity_filter()` (bot/core/
hedonic_constants.py) сочтёт активными на `t0` (first_seen<=t0<=
archived_at или ещё не архивировано).

**Известное ограничение** (см. docstring `hedonic_constants._activity_
filter`): price/area/rooms кандидатов — ТЕКУЩИЕ значения строк
apartment_listings, НЕ значения на дату `t0` (price_history для
цены-на-дату не джойнится). Для площади/комнат это не проблема (не
меняются после публикации), для цены — приближение, допустимое для
старта backtest'а, не полная историческая точность.

Разовый прогон:
  venv/bin/python -m bot.jobs.deal_score_backtest --as-of 2026-07-25
  venv/bin/python -m bot.jobs.deal_score_backtest --as-of 2026-07-25T12:00:00 --listing-id 123 --listing-id 456
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("deal_score_backtest")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")


def _parse_as_of(value: str) -> datetime:
    """'YYYY-MM-DD' или полный ISO datetime; наивные даты трактуются как
    UTC (first_seen/archived_at в БД — timestamptz)."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def run_backtest(as_of: datetime, listing_ids: list[str] | None = None) -> dict[str, dict]:
    """Основная точка входа (импортируемая, не только CLI) — read-only,
    ничего не пишет. apply_deal_scores(as_of=...) уже сам гарантирует
    это при заданном as_of; фильтр по listing_ids — постфактум в Python
    (apply_deal_scores не принимает список id, не усложняем его SQL ради
    этого — фильтрация после получения полного as-of среза дешева)."""
    from bot.core.deal_score import apply_deal_scores
    result = await apply_deal_scores(as_of=as_of)
    if listing_ids is not None:
        wanted = set(listing_ids)
        result = {lid: r for lid, r in result.items() if lid in wanted}
    return result


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--as-of", required=True, help="дата backtest: YYYY-MM-DD или ISO datetime")
    parser.add_argument("--listing-id", action="append", default=None,
                         help="ограничить конкретными listing_id (можно повторять); "
                              "по умолчанию — все объявления, активные на --as-of")
    args = parser.parse_args()
    as_of = _parse_as_of(args.as_of)

    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    try:
        result = await run_backtest(as_of, args.listing_id)
    finally:
        await close_pool()

    log.info("backtest as_of=%s: %d объявлений посчитано, В БД НЕ ЗАПИСАНО",
              as_of.isoformat(), len(result))
    for lid, r in list(result.items())[:5]:
        log.info("  %s: deal=%s confidence=%s flags=%s", lid, r["deal"], r["confidence"], r["flags"])

    if args.listing_id:
        # Точечный запрос по конкретным id — небольшой результат, удобно
        # дампнуть целиком для сравнения. Без --listing-id результат может
        # быть тысячи объявлений — только сводка в лог выше, не JSON-дамп.
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
