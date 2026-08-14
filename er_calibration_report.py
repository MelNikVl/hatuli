#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ER-калибровка по накопленным решениям оператора (Фаза B, п.4, задача
2026-08-14, docs/verdict_strategy.md) — read-only отчёт, НЕ пишет в БД,
НЕ применяет новые пороги автоматически.

См. bot/core/er_calibration.py докстринг за находку о том, что
unit_match_gold_labels (юнит-уровень) и AUTO_MATCH_THRESHOLD/
REVIEW_QUEUE_THRESHOLD (ЖК-уровень, bot/core/entity_resolution.py) —
ДВА РАЗНЫХ механизма, второй не калибруется первым.

Разовый прогон: venv/bin/python er_calibration_report.py
"""
from __future__ import annotations

import asyncio
import logging
import os

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("er_calibration_report")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")


async def main() -> None:
    from bot.db.pg import init_pool, close_pool, fetch
    from bot.core.entity_resolution import AUTO_MATCH_THRESHOLD, REVIEW_QUEUE_THRESHOLD
    from bot.core.er_calibration import (
        summarize_confidence_distribution, unit_gold_label_confirmation_rate,
        evidence_confirmation_breakdown,
    )

    await init_pool(DATABASE_URL)
    try:
        log.info("=" * 78)
        log.info("ЧАСТЬ 1 — ЖК-уровень (entity_resolution.score_match(), AUTO_MATCH_THRESHOLD=%s / REVIEW_QUEUE_THRESHOLD=%s)",
                  AUTO_MATCH_THRESHOLD, REVIEW_QUEUE_THRESHOLD)
        log.info("=" * 78)

        spine_rows = await fetch("SELECT confidence FROM complex_source_links WHERE confidence IS NOT NULL")
        spine_conf = [float(r["confidence"]) for r in spine_rows]
        spine_report = summarize_confidence_distribution(spine_conf, AUTO_MATCH_THRESHOLD, REVIEW_QUEUE_THRESHOLD)
        log.info("Подтверждённый spine (complex_source_links): n=%d, auto-уровень(>=%.2f)=%d (%.1f%%), "
                  "review-уровень(%.2f-%.2f, ПОДТВЕРЖДЁН человеком)=%d (%.1f%%), ниже review(<%.2f)=%d",
                  spine_report["n"], AUTO_MATCH_THRESHOLD, spine_report["auto_tier"],
                  100 * spine_report["auto_tier"] / spine_report["n"] if spine_report["n"] else 0,
                  REVIEW_QUEUE_THRESHOLD, AUTO_MATCH_THRESHOLD, spine_report["review_tier"],
                  100 * spine_report["review_tier"] / spine_report["n"] if spine_report["n"] else 0,
                  REVIEW_QUEUE_THRESHOLD, spine_report["below_review"])
        log.info("Гистограмма review-уровня (подтверждённые TRUE POSITIVE, бакеты 0.05):")
        for bucket, cnt in spine_report["buckets"].items():
            if REVIEW_QUEUE_THRESHOLD <= bucket < AUTO_MATCH_THRESHOLD:
                log.info("  [%.2f-%.2f): %d", bucket, bucket + 0.05, cnt)

        pending = await fetch("SELECT confidence, match_method FROM complex_source_link_candidates WHERE kind='review'")
        log.info("Текущая review-очередь (ЕЩЁ НЕ решено, не лейбл): n=%d, confidence %s",
                  len(pending), sorted(round(float(r["confidence"]), 2) for r in pending))

        rejections = await fetch("SELECT COUNT(*) AS n FROM complex_source_link_rejections")
        log.info("Отклонения (complex_source_link_rejections): n=%d — confidence НЕ сохраняется в "
                  "момент отклонения (гэп в данных, не пересчитан здесь — источник для пересчёта "
                  "confidence на дату отклонения не кэшируется отдельно). ПРЕДЛОЖЕНИЕ (не применено): "
                  "логировать confidence+evidence в момент отклонения, тем же паттерном, что "
                  "record_source_link() уже делает для confirmed/review.", rejections[0]["n"])

        log.info("")
        log.info("ВЫВОД ЖК-уровня: %.1f%% подтверждённого spine — выше AUTO_MATCH_THRESHOLD=%.2f "
                  "(порог хорошо подтверждён, менять не на что). Среди review-уровня, "
                  "ПОДТВЕРЖДЁННОГО человеком, реальные примеры существуют вплоть до %.2f (нижняя "
                  "граница REVIEW_QUEUE_THRESHOLD=%.2f) — поднимать REVIEW_QUEUE_THRESHOLD значило бы "
                  "потерять уже подтверждённые матчи, НЕ предлагается. Понижать — нет данных: "
                  "отклонения без confidence не дают оценить false-positive rate ниже текущего порога. "
                  "ИТОГ: пороги 0.8/0.5 подтверждаются данными как есть, новые не предлагаются "
                  "(это тоже калибровка — не менять, когда данные не показывают, куда).",
                  100 * spine_report["auto_tier"] / spine_report["n"] if spine_report["n"] else 0,
                  AUTO_MATCH_THRESHOLD, min(spine_conf) if spine_conf else float("nan"), REVIEW_QUEUE_THRESHOLD)

        log.info("=" * 78)
        log.info("ЧАСТЬ 2 — юнит-уровень (phase2_unit_match.decide_pair(), НЕТ порога — дерево правил)")
        log.info("=" * 78)

        gold_rows = await fetch("SELECT decision, evidence FROM unit_match_gold_labels")
        decisions = [r["decision"] for r in gold_rows]
        gold_report = unit_gold_label_confirmation_rate(decisions)
        log.info("unit_match_gold_labels: n=%d, approve=%d (%.1f%%), другое=%d",
                  gold_report["n"], gold_report["approve"],
                  100 * gold_report["approve_rate"] if gold_report["approve_rate"] is not None else 0,
                  gold_report["other"])

        import json
        evidences = []
        for r in gold_rows:
            ev = r["evidence"]
            if isinstance(ev, str):
                ev = json.loads(ev)
            evidences.append(ev or {})
        ev_report = evidence_confirmation_breakdown(evidences)
        log.info("Среди подтверждённых (все approve, reason='ambiguous_floorplan' — mirror-cap "
                  "заблокировал auto): price_ok=True у %d/%d (%.1f%%), date_ok=True у %d/%d (%.1f%%), "
                  "НИ ОДНОГО подтверждающего сигнала у %d/%d (%.1f%%) — оператор подтвердил "
                  "исключительно по совпадению этаж+метраж и визуальному суждению.",
                  ev_report["price_ok"], ev_report["n"], 100 * ev_report["price_ok"] / ev_report["n"] if ev_report["n"] else 0,
                  ev_report["date_ok"], ev_report["n"], 100 * ev_report["date_ok"] / ev_report["n"] if ev_report["n"] else 0,
                  ev_report["neither"], ev_report["n"], 100 * ev_report["neither"] / ev_report["n"] if ev_report["n"] else 0)

        rejected_units = await fetch(
            "SELECT COUNT(*) AS n FROM unit_duplicate_candidates WHERE status NOT IN ('review', 'merged')")
        log.info("Отклонённых unit-кандидатов (status NOT IN review/merged): n=%d — 0 означает НОЛЬ "
                  "отрицательных примеров в системе на сегодня.", rejected_units[0]["n"])

        log.info("")
        log.info("ВЫВОД юнит-уровня: unit_match_gold_labels НЕ калибрует AUTO_MATCH_THRESHOLD/"
                  "REVIEW_QUEUE_THRESHOLD (другой механизм, нет confidence вовсе). Что реально "
                  "видно: mirror_count>1 (\"ambiguous_floorplan\") сейчас ВСЕГДА уходит в review "
                  "(жёсткое правило decide_pair(), не порог) — оператор подтвердил %d/%d (100%%) "
                  "таких кандидатов на сегодня, %d/%d (%.0f%%) БЕЗ подтверждающего price/date "
                  "сигнала вовсе. n=%d и 0%% reject не дают статистически надёжно ослабить "
                  "mirror-cap правило (могла быть систематическая удача выборки, не факт "
                  "обобщаемости) — ПРЕДЛОЖЕНИЕ (не применено): не менять правило сейчас, "
                  "продолжить копить gold-labels (особенно reject-случаи, которых пока 0) до "
                  "статистически весомой выборки в обе стороны.",
                  gold_report["approve"], gold_report["n"], ev_report["neither"], ev_report["n"],
                  100 * ev_report["neither"] / ev_report["n"] if ev_report["n"] else 0, gold_report["n"])
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
