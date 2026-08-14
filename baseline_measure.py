#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Baseline-замер (Фаза A, п.3 вердикт-стратегии — ЗАГЛАВНЫЙ артефакт
фазы, docs/verdict_strategy.md §5): предсказательная сила ТЕКУЩЕГО
score_total и его компонентов (price/quality/market/risk/location) по
outcome_labels — AUC на disappeared_within_30d, корреляция (Spearman) с
time_on_market. Отвечает на вопрос "чего стоит текущая эвристика", ДО
Фазы C (модель имеет смысл сравнивать с этим числом, не раньше).

Не пишет в БД — чистое чтение (score_total/hex_details + outcome_labels),
разовый прогон, результат идёт в отчёт (docs/scoring_roadmap.md).

AUC считается через Манна-Уитни U (scipy.stats.mannwhitneyu) —
AUC = U / (n_pos * n_neg), эквивалент площади под ROC без явного
перебора порогов, устойчиво к тому, что score_total — целое 0-100
(много связанных значений).

Скоуп: активная вторичка (market_type='secondary', is_duplicate=false,
score_total не пусто) — то же подмножество, для которого вообще
определён и означает что-то Deal Score v4 (см. scoring_audit.md §2.3).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os

import numpy as np
from scipy import stats

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("baseline_measure.log", encoding="utf-8", errors="replace")],
)
log = logging.getLogger("baseline_measure")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

QUERY = """
    SELECT a.id, a.score_total, a.deal_confidence, a.hex_details,
           o.disappeared_within_30d, o.time_on_market
    FROM apartment_listings a
    JOIN outcome_labels o ON o.listing_id = a.id
    WHERE a.market_type = 'secondary'
      AND COALESCE(a.is_duplicate, FALSE) = FALSE
      AND a.score_total IS NOT NULL
"""


def auc_mannwhitney(scores: np.ndarray, labels: np.ndarray) -> tuple[float | None, int, int]:
    """AUC (вероятность, что случайный TRUE ранжирован выше случайного
    FALSE) + размеры классов. None, если один из классов пуст или все
    значения совпадают (AUC не определён)."""
    pos = scores[labels]
    neg = scores[~labels]
    n_pos, n_neg = len(pos), len(neg)
    if n_pos == 0 or n_neg == 0:
        return None, n_pos, n_neg
    u_stat, _ = stats.mannwhitneyu(pos, neg, alternative="two-sided")
    return float(u_stat / (n_pos * n_neg)), n_pos, n_neg


async def main() -> None:
    from bot.db.pg import init_pool, close_pool, fetch
    await init_pool(DATABASE_URL)
    try:
        rows = await fetch(QUERY)
    finally:
        await close_pool()

    n_total = len(rows)
    score_total = np.array([r["score_total"] for r in rows], dtype=float)
    deal_conf = np.array([r["deal_confidence"] if r["deal_confidence"] is not None else np.nan for r in rows], dtype=float)
    disappeared = np.array([r["disappeared_within_30d"] for r in rows])
    tom = np.array([r["time_on_market"] if r["time_on_market"] is not None else np.nan for r in rows], dtype=float)

    components: dict[str, list[float]] = {"price": [], "quality": [], "market": [], "risk": [], "location": []}
    for r in rows:
        hd = r["hex_details"]
        parsed = json.loads(hd) if hd else {}
        comp = parsed.get("components", {}) if isinstance(parsed, dict) else {}
        for k in components:
            v = comp.get(k, {}).get("score")
            components[k].append(v if v is not None else np.nan)
    for k in components:
        components[k] = np.array(components[k], dtype=float)

    log.info("=" * 70)
    log.info("BASELINE-ЗАМЕР (Фаза A п.3) — выборка: %d активных вторичных объявлений", n_total)
    log.info("=" * 70)

    resolved_mask = ~np.isnan(disappeared.astype(float)) if disappeared.dtype != bool else np.ones(n_total, dtype=bool)
    # asyncpg возвращает None для NULL bool -> при построении np.array с dtype=object;
    # пересобираем маску явно из исходных Record'ов, чтобы не гадать по dtype.
    resolved_idx = np.array([r["disappeared_within_30d"] is not None for r in rows])
    label_bool = np.array([bool(r["disappeared_within_30d"]) if r["disappeared_within_30d"] is not None else False for r in rows])

    n_resolved = int(resolved_idx.sum())
    n_true = int(label_bool[resolved_idx].sum())
    log.info("disappeared_within_30d: разрешено %d/%d (%.1f%%), из них TRUE(быстрый архив без снижений)=%d (%.1f%%)",
              n_resolved, n_total, 100 * n_resolved / n_total if n_total else 0,
              n_true, 100 * n_true / n_resolved if n_resolved else 0)

    log.info("-" * 70)
    log.info("AUC (score_total и компонентов) на disappeared_within_30d — гипотеза: выше score -> вероятнее TRUE")
    for name, arr in [("score_total", score_total), ("deal_confidence", deal_conf),
                       ("price", components["price"]), ("quality", components["quality"]),
                       ("market", components["market"]), ("risk", components["risk"]),
                       ("location (weight=0)", components["location"])]:
        valid = resolved_idx & ~np.isnan(arr)
        if valid.sum() < 10:
            log.info("  %-20s недостаточно данных (n=%d)", name, int(valid.sum()))
            continue
        auc, n_pos, n_neg = auc_mannwhitney(arr[valid], label_bool[valid])
        log.info("  %-20s AUC=%.4f  (n_true=%d, n_false=%d, n=%d)", name,
                  auc if auc is not None else float("nan"), n_pos, n_neg, int(valid.sum()))

    log.info("-" * 70)
    log.info("Корреляция (Spearman) со time_on_market — только разрешённые (архивные, не censored)")
    tom_valid = ~np.isnan(tom)
    n_tom = int(tom_valid.sum())
    log.info("time_on_market известен для %d/%d (%.1f%%) — остальные censored (ещё активны)",
              n_tom, n_total, 100 * n_tom / n_total if n_total else 0)
    for name, arr in [("score_total", score_total), ("deal_confidence", deal_conf),
                       ("price", components["price"]), ("quality", components["quality"]),
                       ("market", components["market"]), ("risk", components["risk"])]:
        valid = tom_valid & ~np.isnan(arr)
        if valid.sum() < 10:
            log.info("  %-20s недостаточно данных (n=%d)", name, int(valid.sum()))
            continue
        rho, p = stats.spearmanr(arr[valid], tom[valid])
        log.info("  %-20s Spearman rho=%.4f  p=%.4g  (n=%d)", name, rho, p, int(valid.sum()))

    log.info("-" * 70)
    log.info("ОГОВОРКА: survives_90d НЕ используется здесь — датасет ещё не старше ~70 дней "
              "(first_seen с 2026-06-05), 90-дневное окно физически не может дать ни одного "
              "TRUE на эту дату (см. outcome_labels_recompute.py докстринг) — честно пропущено, не гадаем.")
    log.info("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
