#!/usr/bin/env python3
"""
Одноразовый бэкфилл: досчитывает score_floor / score_complex у легаси-строк,
где эти колонки NULL, но исходные данные (floor/floors_total/year_built)
уже есть в БД. Ничего заново не парсит — только пересчёт по существующим
данным. Безопасно запускать повторно (трогает только строки с NULL).

Запуск:  python backfill_scores.py
"""
from __future__ import annotations

import asyncio
import logging
import os

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("backfill")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")


async def main() -> None:
    from bot.core.apartment_score_v2 import floor_score, complex_score
    from bot.db.pg import init_pool, execute as pg_exec, fetch as pg_fetch

    await init_pool(DATABASE_URL)

    rows = await pg_fetch(
        """
        SELECT id, floor, floors_total, year_built, complex_name,
               score_floor, score_complex, score_total
        FROM apartment_listings
        WHERE score_floor IS NULL OR score_complex IS NULL
        """
    )
    log.info("Найдено %d строк с NULL score_floor/score_complex", len(rows))

    updated = 0
    for row in rows:
        r = dict(row)
        old_floor = r["score_floor"] or 0
        old_complex = r["score_complex"] or 0

        new_floor, _ = floor_score(r["floor"], r["floors_total"])
        new_complex, _ = complex_score(r["year_built"], r["complex_name"])

        old_total = r["score_total"] or 0
        # снимаем старый (0, если был NULL) вклад и добавляем новый, капаем в 100
        new_total = min(100, max(0, old_total - old_floor - old_complex + new_floor + new_complex))

        await pg_exec(
            """
            UPDATE apartment_listings
            SET score_floor=$2, score_complex=$3, score_total=$4
            WHERE id=$1
            """,
            r["id"], new_floor, new_complex, new_total,
        )
        updated += 1
        if updated % 100 == 0:
            log.info("...%d/%d", updated, len(rows))

    log.info("Готово: обновлено %d строк", updated)


if __name__ == "__main__":
    asyncio.run(main())
