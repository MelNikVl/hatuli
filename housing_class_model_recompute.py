#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Класс-модель ЖК — обучение + применение (Фаза B, п.3, задача
2026-08-14, docs/verdict_strategy.md).

**НАЗНАЧЕНИЕ (честно ограничено)**: подготовка данных для будущей
стратификации аналогов и ML-моделей Фазы C, НЕ попытка поднять
price_score AUC — см. bot/core/housing_class_model.py докстринг и
"Анализ потолка price_score" в docs/verdict_strategy.md.

Обучает Gaussian Naive Bayes (bot/core/housing_class_model) на ручных
лейблах complexes.housing_class (нормализованных через normalize_label —
"премиум" и подобные, не входящие в 4-тиерную таксономию, честно
исключаются), логирует holdout-метрики, применяет финальную модель
(обучена на ВСЕЙ размеченной выборке, не train-сплите) ко всем
complexes с известными avg_price_m2+year_built.

predicted_housing_class_source:
  'manual'    — housing_class уже заполнен руками, предсказание не
                считается (ручная метка приоритетнее), probability=NULL.
  'predicted' — модель дала предсказание.
  NULL        — признаков недостаточно (avg_price_m2 или year_built
                неизвестны) — Unknown ≠ average, не гадаем.

Расписание: разовый прогон на дату задачи, ongoing — по мере роста
разметки (не привязан к таймеру, малый объём данных, вручную).
Разовая проверка: venv/bin/python housing_class_model_recompute.py
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("housing_class_model_recompute.log", encoding="utf-8", errors="replace")],
)
log = logging.getLogger("housing_class_model_recompute")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")


async def run_recompute() -> dict:
    from bot.db.pg import fetch, execute
    from bot.core.housing_class_model import normalize_label, train, predict, evaluate_holdout

    rows = await fetch("""
        SELECT id, housing_class, avg_price_m2, year_built
        FROM complexes WHERE COALESCE(is_garbage, FALSE) = FALSE
    """)

    labeled = []
    for r in rows:
        cls = normalize_label(r["housing_class"])
        if cls and r["avg_price_m2"] and r["year_built"]:
            labeled.append((cls, float(r["avg_price_m2"]), r["year_built"]))

    unmapped = sum(1 for r in rows if r["housing_class"] and not normalize_label(r["housing_class"]))
    log.info("живая выборка: %d complexes, %d с ручной меткой И известными признаками "
              "(из них %d меток не смаппились в 4-тиерную таксономию, честно исключены)",
              len(rows), len(labeled), unmapped)

    holdout_report = evaluate_holdout(labeled)
    log.info("HOLDOUT: n_train=%d n_holdout=%d accuracy=%s",
              holdout_report["n_train"], holdout_report["n_holdout"],
              f"{holdout_report['accuracy']:.3f}" if holdout_report["accuracy"] is not None else "n/a")
    for cls, m in holdout_report["per_class"].items():
        log.info("  %-8s precision=%s recall=%s n_holdout=%d", cls,
                  f"{m['precision']:.3f}" if m["precision"] is not None else "n/a",
                  f"{m['recall']:.3f}" if m["recall"] is not None else "n/a",
                  m["n_holdout"])

    # Финальная модель — на ВСЕЙ размеченной выборке (не train-сплите
    # holdout-оценки выше) — это то, что реально применяется к базе.
    final_model = train(labeled)

    now = datetime.now(timezone.utc)
    manual_n = predicted_n = unknown_n = 0
    for r in rows:
        manual_cls = normalize_label(r["housing_class"])
        if manual_cls:
            await execute("""
                UPDATE complexes SET predicted_housing_class=NULL, predicted_housing_class_probability=NULL,
                    predicted_housing_class_source='manual', predicted_housing_class_computed_at=$2
                WHERE id=$1
            """, r["id"], now)
            manual_n += 1
            continue
        pred_cls, prob = predict(final_model, r["avg_price_m2"], r["year_built"])
        if pred_cls is not None:
            await execute("""
                UPDATE complexes SET predicted_housing_class=$2, predicted_housing_class_probability=$3,
                    predicted_housing_class_source='predicted', predicted_housing_class_computed_at=$4
                WHERE id=$1
            """, r["id"], pred_cls, prob, now)
            predicted_n += 1
        else:
            await execute("""
                UPDATE complexes SET predicted_housing_class=NULL, predicted_housing_class_probability=NULL,
                    predicted_housing_class_source=NULL, predicted_housing_class_computed_at=$2
                WHERE id=$1
            """, r["id"], now)
            unknown_n += 1

    log.info("применено: manual=%d predicted=%d unknown(признаков не хватило)=%d всего=%d",
              manual_n, predicted_n, unknown_n, len(rows))
    return {"holdout": holdout_report, "manual": manual_n, "predicted": predicted_n,
            "unknown": unknown_n, "total": len(rows)}


async def main() -> None:
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    try:
        await run_recompute()
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
