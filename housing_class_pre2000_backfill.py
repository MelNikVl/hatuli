#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Эвристика класса старого фонда (задача 2026-08-14, read-only-сессия
п.4, docs/liquidity_model_design.md §11) — одноразовый backfill,
НЕ таймер (не пересчитывается регулярно, в отличие от housing_class_
estimate_recompute.py/housing_class_model_recompute.py).

Правило: year_built < 2000 AND housing_class IS NULL -> housing_class =
'эконом'. Обоснование и честная проверка масштаба — см. docs/liquidity_
model_design.md §11: заявленная предпосылка "~95% домов старше 2000г. —
советский фонд" НЕ подтвердилась для этой БД (Астана застраивалась в
основном ПОСЛЕ 2000г.) — эвристика закрывает малую часть дыры покрытия
класса (34 из 1837 записей без housing_class на дату написания, ≈1.9%),
не "основную дыру". Применяется как есть — для этих 34 записей
присвоение разумно и низкорискованно, просто не переоцениваем эффект.

housing_class_source='pre2000_heuristic' (migrations/073) различает эти
записи от вручную заполненных — **но bot/core/housing_class_model_
recompute.py на housing_class_source не смотрит** и после этого
бэкфилла будет считать все 34 строки обычной ручной меткой (см.
докстринг миграции 073) — известное ограничение, не исправлено здесь.

Расписание: НЕТ таймера — одноразовый прогон.
Разовая проверка: venv/bin/python housing_class_pre2000_backfill.py [--dry]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.FileHandler("housing_class_pre2000_backfill.log", encoding="utf-8", errors="replace")],
)
log = logging.getLogger("housing_class_pre2000_backfill")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

HEURISTIC_YEAR_CUTOFF = 2000
HEURISTIC_LABEL = "эконом"
HEURISTIC_SOURCE = "pre2000_heuristic"


async def run_backfill(dry: bool = False) -> dict:
    from bot.db.pg import fetch, execute

    targets = await fetch("""
        SELECT id FROM complexes
        WHERE COALESCE(is_garbage, FALSE) = FALSE
          AND year_built IS NOT NULL AND year_built < $1
          AND housing_class IS NULL
    """, HEURISTIC_YEAR_CUTOFF)

    if not dry and targets:
        ids = [r["id"] for r in targets]
        await execute("""
            UPDATE complexes
            SET housing_class = $2, housing_class_source = $3, housing_class_source_computed_at = now()
            WHERE id = ANY($1::int[])
        """, ids, HEURISTIC_LABEL, HEURISTIC_SOURCE)

    result = {"targets": len(targets), "updated": 0 if dry else len(targets), "dry_run": dry}
    log.info("housing_class_pre2000_backfill: %d ЖК (year_built<%d, housing_class IS NULL), обновлено %d%s",
              result["targets"], HEURISTIC_YEAR_CUTOFF, result["updated"], " [DRY RUN]" if dry else "")
    return result


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    try:
        await run_backfill(dry=args.dry)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
