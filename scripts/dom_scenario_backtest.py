#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/dom_scenario_backtest.py — честный временной backtest НОВОГО
эмпирического метода (bot/analytics/dom_scenario.py: Kaplan-Meier по
сегменту с фоллбэком + PAVA-сглаживание ценовых корзин) против уже
проверенного сегментного baseline (задача 2026-08-21, "MVP прогноза срока
экспозиции при разных ценах", §5 задания).

НЕ переписывает и не заменяет scripts/dom_forecast_baseline_backtest.py —
тот результат (AFT в 10-17 раз хуже baseline, docs/dom_forecast_audit.md)
остаётся как есть. Этот скрипт — ОТДЕЛЬНАЯ проверка нового, непараметрического
метода, который в итоге и подключён к UI (в отличие от AFT).

Схема backtest — та же временная схема, что уже проверена в оригинальном
скрипте (переиспользуется 1:1 — _fetch_rows/_build_records/REAL_DISTRICTS/
_baseline_segment_median импортируются оттуда, не дублируются):
  cutoff = 2026-08-01. train: property с first_seen < cutoff, event=1 только
  если archived_at < cutoff И time_on_market уже известен НА cutoff, иначе
  censored (T = cutoff - first_seen). test: та же выборка, которая РЕАЛЬНО
  разрешилась ПОСЛЕ cutoff — сравниваем прогноз "как если бы мы стояли на
  cutoff" с фактическим исходом. Ни один признак теста не использует данные
  после cutoff (то же свойство "нет утечки будущего", что и в оригинале).

Метод здесь — ТЕ ЖЕ функции, что использует продовый bot/analytics/
dom_scenario.py (kaplan_meier/km_quantile/pava/_price_sensitivity_curve/
_enforce_monotone_scenarios) — не переизобретаются заново для backtest,
единственная реализация используется и в проде, и здесь.

Дополнительно (то, чего у AFT-скрипта не было) — проверка монотонности
сценариев на пробе test-записей: снижение цены не может увеличивать
прогнозный срок (см. блок "MONOTONICITY CHECK" в выводе).

Не пишет в БД. Единственный побочный эффект — печать отчёта в stdout."""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv

load_dotenv()

from dom_forecast_baseline_backtest import (  # noqa: E402 — переиспользуем 1:1
    _fetch_rows, _build_records, _baseline_segment_median,
)
from bot.analytics.dom_scenario import (  # noqa: E402
    kaplan_meier, km_quantile, pava, _price_sensitivity_curve, _interp_curve,
    _enforce_monotone_scenarios, _clamp_days, DAYS_MIN, DAYS_MAX,
    MIN_EVENTS_FOR_KM, MIN_EVENTS_MEDIUM, DISCOUNT_SCENARIOS,
)

MIN_EVENTS_FOR_TIER = MIN_EVENTS_MEDIUM


def _pick_km_segment(records: list[dict], target: dict):
    """Тот же фоллбэк, что _pick_segment в bot/analytics/dom_scenario.py,
    упрощённый до 3 уровней — records уже приходят с УКРУПНЁННОЙ
    комнатностью (`rooms` — результат _rooms_bucket в _build_records
    оригинального скрипта, точная комнатность там не сохраняется), поэтому
    уровень "district × точная комнатность" здесь не воспроизводим отдельно
    от "district × укрупнённая" — это не искажает сравнение с baseline (тот
    же нюанс данных действует и на сам baseline ниже)."""
    tiers = [
        ("district_rooms_bucket", lambda r: r["district"] == target["district"] and r["rooms"] == target["rooms"]),
        ("city_rooms_bucket", lambda r: r["rooms"] == target["rooms"]),
        ("city_baseline", lambda r: True),
    ]
    best = None
    for name, pred in tiers:
        pop = [r for r in records if pred(r)]
        obs = [(r["T_train"], r["event_train"]) for r in pop]
        event_count = sum(1 for _, e in obs if e == 1)
        best = (name, pop, obs, event_count)
        if event_count >= MIN_EVENTS_FOR_TIER:
            return best
    return best


def _km_point_prediction(obs: list[tuple[float, int]], records_pop: list[dict]):
    """Возвращает (days_pred, method) — медиана KM, если кривая её
    достигает и событий достаточно, иначе baseline-фоллбэк (медиана T
    среди событий) — тот же принцип понижения точности, что и в проде
    (compute_dom_scenario, п.10 задания)."""
    event_count = sum(1 for _, e in obs if e == 1)
    if event_count >= MIN_EVENTS_FOR_KM:
        steps = kaplan_meier(obs)
        median = km_quantile(steps, 0.5)
        if median is not None:
            return _clamp_days(median), "kaplan_meier"
    event_Ts = sorted(t for t, e in obs if e == 1)
    if not event_Ts:
        return None, "no_events"
    return _clamp_days(event_Ts[len(event_Ts) // 2]), "segment_median_baseline"


async def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cutoff", default="2026-08-01", help="Дата раздела train/test (YYYY-MM-DD)")
    args = ap.parse_args()
    cutoff = datetime.strptime(args.cutoff, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    raw = await _fetch_rows()
    print(f"Загружено property-строк: {len(raw)}")
    records = _build_records(raw, cutoff)
    n_events = sum(1 for r in records if r["event_train"] == 1)
    test_recs = [r for r in records if r["really_resolved_after_cutoff"]]
    print(f"train N={len(records)} (event={n_events}, censored={len(records)-n_events}); "
          f"честный test N={len(test_recs)} (cutoff={args.cutoff})")
    if n_events < 100 or len(test_recs) < 30:
        print("Недостаточно данных даже для содержательного backtest — остановка.")
        return

    baseline_pred = _baseline_segment_median(records)

    tier_counts = defaultdict(int)
    method_counts = defaultdict(int)
    km_preds = []
    valid_test_recs = []
    for rec in test_recs:
        tier_name, pop, obs, event_count = _pick_km_segment(records, rec)
        pred, method = _km_point_prediction(obs, pop)
        if pred is None:
            continue
        tier_counts[tier_name] += 1
        method_counts[method] += 1
        km_preds.append(pred)
        valid_test_recs.append(rec)

    actuals = np.array([r["true_tom"] for r in valid_test_recs])
    km_preds = np.array(km_preds)
    base_preds = np.array([baseline_pred(r) for r in valid_test_recs])

    def mae(p):
        return float(np.mean(np.abs(p - actuals)))

    def medae(p):
        return float(np.median(np.abs(p - actuals)))

    print(f"\nСегменты, выбранные для test-записей: {dict(tier_counts)}")
    print(f"Метод предсказания: {dict(method_counts)}")

    print(f"\n=== BACKTEST (N={len(valid_test_recs)}) ===")
    print(f"Kaplan-Meier + fallback:  MAE={mae(km_preds):.1f} дн  medAE={medae(km_preds):.1f} дн  "
          f"pred range=[{km_preds.min():.1f}, {km_preds.max():.1f}]")
    print(f"Baseline (сегм. медиана): MAE={mae(base_preds):.1f} дн  medAE={medae(base_preds):.1f} дн")
    verdict = "ПРОШЁЛ (лучше или равно baseline)" if mae(km_preds) <= mae(base_preds) else "НЕ ПРОШЁЛ (хуже baseline)"
    print(f"\nQuality gate (KM должен быть не хуже baseline): {verdict}")
    print("Примечание: KM — тот же класс метода, что и baseline (непараметрическая "
          "медиана по сегменту), поэтому ожидаемый результат — БЛИЗКИЙ к baseline, "
          "не драматическое улучшение (в отличие от AFT, который был на порядок хуже) "
          "— см. docs/dom_forecast_audit.md §5 п.4.")

    # ── MONOTONICITY CHECK — снижение цены не может увеличить срок ──────
    print("\n=== MONOTONICITY CHECK (сценарии по цене, проба из test-записей) ===")
    sample = valid_test_recs[:min(20, len(valid_test_recs))]
    violations = 0
    checked = 0
    for rec in sample:
        tier_name, pop, obs, event_count = _pick_km_segment(records, rec)
        base_pred, _ = _km_point_prediction(obs, pop)
        if base_pred is None:
            continue
        days_low_base = _clamp_days(base_pred * 0.8)
        days_high_base = _clamp_days(base_pred * 1.3)
        events_dev = [(r["ppm2"], t) for r, (t, e) in zip(pop, obs) if e == 1]
        # price_dev относительно медианы ppm2 сегмента (та же формула, что в проде)
        seg_ppm2 = sorted(r["ppm2"] for r in pop)
        if not seg_ppm2:
            continue
        seg_median_ppm2 = seg_ppm2[len(seg_ppm2) // 2]
        events_dev = [(math.log(max(ppm2, 1e-6)) - math.log(max(seg_median_ppm2, 1e-6)), t)
                      for ppm2, t in events_dev]
        curve = _price_sensitivity_curve(events_dev)
        current_dev = math.log(max(rec["ppm2"], 1e-6)) - math.log(max(seg_median_ppm2, 1e-6))
        baseline_val = _interp_curve(curve, current_dev) if curve else None
        scenarios = []
        for pct in DISCOUNT_SCENARIOS:
            scenario_ppm2 = rec["ppm2"] * (1 - pct / 100)
            target_dev = math.log(max(scenario_ppm2, 1e-6)) - math.log(max(seg_median_ppm2, 1e-6))
            multiplier = (_interp_curve(curve, target_dev) / baseline_val) if (curve and baseline_val) else 1.0
            scenarios.append({
                "discount_pct": pct, "price": rec["price"] * (1 - pct / 100),
                "days_low": _clamp_days(days_low_base * multiplier),
                "days_high": _clamp_days(days_high_base * multiplier),
            })
        fixed = _enforce_monotone_scenarios(scenarios)
        checked += 1
        lows = [s["days_low"] for s in fixed]
        highs = [s["days_high"] for s in fixed]
        if not (all(lows[i] <= lows[i - 1] for i in range(1, len(lows)))
                and all(highs[i] <= highs[i - 1] for i in range(1, len(highs)))):
            violations += 1
    print(f"Проверено {checked} записей, нарушений монотонности: {violations} "
          f"(ожидается 0 — _enforce_monotone_scenarios гарантирует это структурно)")


if __name__ == "__main__":
    asyncio.run(main())
