#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Дозаполнение completion_year/completion_quarter у complexes.is_newbuild
(Часть 0, задача 2026-08-14, "быстрые победы") — тег переуступка/вторичка
(bot/core/newbuild_person_offers.classify_person_offer) для 66.6% людских
объявлений на страницах новостроек не проставлялся вовсе именно потому,
что срок сдачи ЖК неизвестен (был известен только у 92 из 192 is_newbuild
ЖК, 48%, см. отчёт по тегу переуступки в предыдущей задаче).

Источники, проверенные на живых данных ПЕРЕД реализацией (см. отчёт ниже) —
только homeportal реально даёт дату по неохваченным ЖК:
  - homeportal_objects.commissioning_date (DD.MM.YYYY, официальные данные
    КЖК) — 24 ЖК из 100 неохваченных имеют матч с непустой датой.
  - developer-direct (newbuild_units.raw_json) — проверено на живых
    данных: 8 неохваченных ЖК с newbuild_source, ВСЕ 'bazis' — его
    raw_json содержит только поюнитовую шахматку (этаж/цена/площадь),
    ни разу нет поля срока сдачи ЖК. Источник физически не отдаёт эти
    данные в том, что мы собираем — не бэкфил, потребовало бы расширять
    сам скрапер bazis_import.py (отдельная задача, не эта).
  - korter (complexes.source_info->korter) — проверено на живых данных:
    ни один сохранённый JSON (по всей таблице complexes, не только
    неохваченным) не содержит поля даты сдачи (только name/price_from/
    housing_class/district/url/developer/price_m2) — korter_import.py
    его не парсит с сайта вовсе (grep по "deadline"/"сдач"/"commission"
    — 0 совпадений). Тот же случай, что bazis — источник не отдаёт эти
    данные в текущем виде сбора.

Итог: из 3 названных источников реально применим только homeportal —
единственный, где данные УЖЕ собраны и просто не долетели до
complexes.completion_year.

Квартал определяется по месяцу commissioning_date (1-3→Q1, 4-6→Q2,
7-9→Q3, 10-12→Q4) — тот же грануляр, что completion_quarter у
developer-direct источников (1..4, SMALLINT).

Идемпотентно: WHERE completion_year IS NULL, повторный запуск не
перезаписывает уже заполненное (в т.ч. руками) значение.

Запуск: venv/bin/python complex_completion_year_backfill.py [--dry]
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
)
log = logging.getLogger("completion_year_backfill")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")


def _parse_commissioning_date(s: str) -> tuple[int, int] | None:
    """'DD.MM.YYYY' -> (year, quarter). None, если формат неожиданный
    (не должен случиться — все живые значения на момент задачи ему
    соответствуют, но не доверяем источнику молча)."""
    parts = s.strip().split(".")
    if len(parts) != 3:
        return None
    try:
        day, month, year = int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return None
    if not (1 <= month <= 12):
        return None
    quarter = (month - 1) // 3 + 1
    return year, quarter


async def run_backfill(dry: bool = False) -> dict:
    from bot.db.pg import fetch, execute

    before = await fetch(
        "SELECT count(*) AS total, count(completion_year) AS has_year FROM complexes WHERE is_newbuild")
    before = dict(before[0])

    candidates = await fetch("""
        SELECT c.id,
               (SELECT ho.commissioning_date FROM homeportal_objects ho
                WHERE (ho.matched_complex_id = c.id
                       OR EXISTS (SELECT 1 FROM complex_source_links l
                                  WHERE l.source = 'homeportal' AND l.source_id = ho.object_id::text
                                    AND l.complex_id = c.id))
                  AND ho.commissioning_date IS NOT NULL AND ho.commissioning_date != ''
                ORDER BY ho.object_id LIMIT 1) AS commissioning_date
        FROM complexes c
        WHERE c.is_newbuild AND c.completion_year IS NULL
    """)

    updated = 0
    skipped_bad_format = 0
    for row in candidates:
        if not row["commissioning_date"]:
            continue
        parsed = _parse_commissioning_date(row["commissioning_date"])
        if not parsed:
            skipped_bad_format += 1
            log.warning("complex %s: не смог разобрать commissioning_date=%r",
                        row["id"], row["commissioning_date"])
            continue
        year, quarter = parsed
        if not dry:
            await execute(
                "UPDATE complexes SET completion_year=$2, completion_quarter=$3 "
                "WHERE id=$1 AND completion_year IS NULL",
                row["id"], year, quarter)
        updated += 1

    after = await fetch(
        "SELECT count(*) AS total, count(completion_year) AS has_year FROM complexes WHERE is_newbuild")
    after = dict(after[0])

    result = {
        "before_total": before["total"], "before_has_year": before["has_year"],
        "after_total": after["total"],
        "after_has_year": (before["has_year"] + updated) if dry else after["has_year"],
        "updated": updated, "skipped_bad_format": skipped_bad_format,
        "dry_run": dry,
    }
    log.info(
        "completion_year backfill: было %d/%d (%.0f%%), обновлено %d, стало %d/%d (%.0f%%)%s",
        result["before_has_year"], result["before_total"],
        result["before_has_year"] / result["before_total"] * 100 if result["before_total"] else 0,
        updated,
        result["after_has_year"], result["after_total"],
        result["after_has_year"] / result["after_total"] * 100 if result["after_total"] else 0,
        " [DRY RUN]" if dry else "",
    )
    return result


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="только посчитать, не писать в БД")
    args = ap.parse_args()

    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    try:
        await run_backfill(dry=args.dry)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
