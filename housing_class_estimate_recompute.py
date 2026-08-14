#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Пересчёт complexes.housing_class_estimate (Часть 2, п.11, задача
2026-08-14, "скоринг волна 2") — эвристическая оценка класса жилья
(эконом/комфорт/бизнес/премиум) для ЖК без официального housing_class,
по тем же двум сигналам, что описывает UI-тултип на карточке ЖК
("по медианной высоте потолков и цене/м² объявлений в ЖК относительно
города") — тот, что был заполнен ОДНОРАЗОВЫМ прогоном 2026-08-01
(коммит 0bb2479) и с тех пор не пересчитывался ни разу (docs/
scoring_audit.md §3/§5.2).

ВАЖНО: точная формула того разового прогона нигде не сохранилась (сырой
SQL, не код в репозитории) — это РЕКОНСТРУКЦИЯ по тому же принципу
(перцентиль цены/м² в городе + бонус/штраф за высоту потолков от базы
2.7м, тот же baseline, что deal_score.py._ceiling_adj), не гарантированно
идентичные числа старому разовому прогону. Значения могут заметно
измениться при первом пересчёте — ожидаемо, честная актуальная оценка
важнее совпадения с забытой формулой.

Пишет housing_class_estimate + housing_class_estimate_computed_at
(migrations/063) — ежемесячный пересчёт держит обе метки согласованными
(само значение + дата, на которую оно актуально), не как раньше.

Расписание: krisha-housing-class-estimate.timer (ежемесячно).
Разовая проверка: venv/bin/python housing_class_estimate_recompute.py [--dry]
"""
from __future__ import annotations

import argparse
import asyncio
import bisect
import logging
import os

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.FileHandler("housing_class_estimate_recompute.log", encoding="utf-8", errors="replace")],
)
log = logging.getLogger("housing_class_estimate_recompute")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

# База высоты потолков — тот же baseline, что deal_score.py._ceiling_adj
# (2.7м), не отдельно выдуманное число.
_CEILING_BASELINE = 2.7
_CEILING_BONUS_PER_10CM = 3.0  # баллов к скору за каждые +10см потолков

_LABELS = [(75, "премиум"), (50, "бизнес"), (25, "комфорт"), (0, "эконом")]


def _label_for_score(score: float) -> str:
    for threshold, label in _LABELS:
        if score >= threshold:
            return label
    return "эконом"


def estimate_class(price_percentile: float, ceiling_height: float | None) -> tuple[str, float]:
    """price_percentile: 0..1 (перцентиль avg_price_m2 ЖК среди всех
    ЖК с известной ценой). -> (label, raw_score 0..~130)."""
    score = price_percentile * 100
    if ceiling_height:
        score += (float(ceiling_height) - _CEILING_BASELINE) / 0.10 * _CEILING_BONUS_PER_10CM
    return _label_for_score(score), round(score, 1)


async def run_recompute(dry: bool = False) -> dict:
    from bot.db.pg import fetch, execute

    # Перцентиль цены/м² — среди ВСЕХ complexes с известным avg_price_m2
    # (та же живая статистика, что пересчитывается каждый цикл парсера,
    # см. service_apartments.py) — не выдумываем отдельный источник цены.
    price_rows = await fetch(
        "SELECT id, avg_price_m2 FROM complexes WHERE avg_price_m2 IS NOT NULL AND avg_price_m2 > 0")
    prices_sorted = sorted(float(r["avg_price_m2"]) for r in price_rows)
    price_by_id = {r["id"]: float(r["avg_price_m2"]) for r in price_rows}

    # Медианная высота потолков по ЖК — из apartment_listings.ceiling_height,
    # тот же паттерн привязки (имя ИЛИ resolved_house_id), что everywhere
    # в проекте после урока волны 1 скоринга (House-resolution в скоринге).
    ceiling_rows = await fetch("""
        SELECT c.id,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY al.ceiling_height) AS median_ceiling
        FROM complexes c
        JOIN apartment_listings al
          ON (lower(trim(al.complex_name)) = lower(trim(c.name)) OR al.resolved_house_id = c.id)
        WHERE al.ceiling_height IS NOT NULL AND al.ceiling_height BETWEEN 2.0 AND 4.5
        GROUP BY c.id
    """)
    ceiling_by_id = {r["id"]: float(r["median_ceiling"]) for r in ceiling_rows if r["median_ceiling"]}

    targets = await fetch("""
        SELECT id FROM complexes
        WHERE housing_class IS NULL AND COALESCE(is_garbage, FALSE) = FALSE
    """)

    updated = 0
    covered_before = await fetch(
        "SELECT COUNT(*) AS n FROM complexes WHERE housing_class_estimate IS NOT NULL")
    for row in targets:
        cid = row["id"]
        price = price_by_id.get(cid)
        if price is None:
            continue  # честно не оцениваем без данных о цене — не гадаем
        pct = bisect.bisect_left(prices_sorted, price) / len(prices_sorted)
        label, _score = estimate_class(pct, ceiling_by_id.get(cid))
        if not dry:
            await execute(
                "UPDATE complexes SET housing_class_estimate=$2, "
                "housing_class_estimate_computed_at=now() WHERE id=$1",
                cid, label)
        updated += 1

    covered_after_row = await fetch(
        "SELECT COUNT(*) AS n FROM complexes WHERE housing_class_estimate IS NOT NULL")
    covered_after = covered_before[0]["n"] if dry else covered_after_row[0]["n"]
    result = {
        "targets": len(targets), "updated": updated,
        "covered_before": covered_before[0]["n"], "covered_after": covered_after,
        "dry_run": dry,
    }
    log.info("housing_class_estimate: %d ЖК без housing_class, %d оценено (с ценой), покрытие %d -> %d%s",
              result["targets"], result["updated"], result["covered_before"], result["covered_after"],
              " [DRY RUN]" if dry else "")
    return result


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    try:
        await run_recompute(dry=args.dry)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
