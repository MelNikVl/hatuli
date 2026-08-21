#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/dom_forecast_baseline_backtest.py — read-only прототип +
честный временной backtest прогноза срока экспозиции (DOM) по цене
(задача 2026-08-21, Часть 2). См. docs/dom_forecast_audit.md за полным
разбором результатов и решением (quality gate НЕ пройден на эту дату —
скрипт сохранён для повторного прогона, когда данных станет больше, а
НЕ подключён ни к одному пользовательскому роуту/API).

Единица наблюдения — property_id (Property Identity), не listing_id (по
заданию) — на дату написания релисты почти не встречаются (см. аудит),
поэтому этот выбор архитектурно корректен, но почти не влияет на числа.

Backtest — ВРЕМЕННОЙ, не случайный train/test split:
  train: property с first_seen < CUTOFF. event=1 только если archived_at
         < CUTOFF и time_on_market уже известен НА CUTOFF (честно — не
         подглядываем в будущее). Иначе censored, T = CUTOFF - first_seen.
  test:  та же выборка (first_seen < CUTOFF), которая РЕАЛЬНО разрешилась
         ПОСЛЕ CUTOFF — сравниваем прогноз "как если бы мы стояли на
         CUTOFF" с фактическим исходом, который сегодня уже известен.

Модель — log-normal AFT (accelerated failure time) с right-censoring,
MLE через scipy.optimize (numpy/scipy — тот же инструментарий, что уже
использует baseline_measure.py; sklearn/pandas/lifelines в окружении
проекта нет и не устанавливались намеренно — задача явно просит не
тянуть тяжёлые ML-зависимости туда, где хватает статистики).

Не пишет в БД. Единственный побочный эффект — печать отчёта в stdout.
"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np
from scipy import optimize, stats
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

REAL_DISTRICTS = ["Есильский р-н", "Алматы р-н", "Сарыарка р-н", "Нура р-н", "Сарайшык р-н", "р-н Байконур"]
MIN_SEGMENT_N = 5


def _rooms_bucket(r):
    if r is None:
        return None
    return "4+" if r >= 4 else str(r)


async def _fetch_rows():
    from bot.db.pg import init_pool, fetch, close_pool
    await init_pool(DATABASE_URL)
    try:
        rows = await fetch("""
            WITH ranked AS (
                SELECT a.*, pl.property_id,
                       row_number() OVER (PARTITION BY pl.property_id ORDER BY a.first_seen DESC) AS rn
                FROM apartment_listings a
                JOIN property_listings pl ON pl.listing_id = a.id
                WHERE a.price > 0 AND a.area > 0 AND a.rooms IS NOT NULL
                  AND a.district = ANY($1::text[])
            )
            SELECT r.id AS listing_id, r.property_id, r.price, r.area, r.rooms, r.floor,
                   r.floors_total, r.district, r.market_type, r.first_seen, r.archived_at,
                   r.is_active, ol.time_on_market,
                   (SELECT ph.old_price FROM price_history ph WHERE ph.listing_id = r.id
                    ORDER BY ph.changed_at ASC LIMIT 1) AS earliest_old_price
            FROM ranked r
            LEFT JOIN outcome_labels ol ON ol.listing_id = r.id
            WHERE r.rn = 1
        """, REAL_DISTRICTS)
        return [dict(r) for r in rows]
    finally:
        await close_pool()


def _build_records(raw, cutoff: datetime):
    records = []
    for r in raw:
        fs = r["first_seen"]
        if fs is None or fs >= cutoff:
            continue
        price = r["earliest_old_price"] or r["price"]
        if not price or not r["area"] or r["area"] <= 0:
            continue
        rb = _rooms_bucket(r["rooms"])
        if rb is None:
            continue
        ppm2 = price / r["area"]
        archived_at = r["archived_at"]
        tom = r["time_on_market"]
        known_before_cutoff = archived_at is not None and archived_at < cutoff and tom is not None
        if known_before_cutoff:
            event_train, T_train = 1, max(float(tom), 0.5)
        else:
            event_train, T_train = 0, max((cutoff - fs).total_seconds() / 86400.0, 0.5)
        really_resolved_after = archived_at is not None and archived_at >= cutoff and tom is not None
        records.append(dict(
            price=price, area=r["area"], ppm2=ppm2, rooms=rb, floor=r["floor"],
            floors_total=r["floors_total"], district=r["district"], market_type=r["market_type"],
            event_train=event_train, T_train=T_train,
            really_resolved_after_cutoff=really_resolved_after,
            true_tom=float(tom) if tom is not None else None,
        ))
    return records


def _segment_medians(records):
    seg = defaultdict(list)
    for r in records:
        seg[(r["district"], r["rooms"])].append(r["ppm2"])
    seg_median = {k: float(np.median(v)) for k, v in seg.items() if len(v) >= MIN_SEGMENT_N}
    global_median = float(np.median([r["ppm2"] for r in records]))
    return seg_median, global_median


def _fit_lean_aft(records, seg_median, global_median):
    """Экономная спецификация (price_dev + ln(площадь)) — полная
    спецификация с district/rooms dummy опробована и отброшена: те
    коллинеарны с price_dev (который УЖЕ нормирован на сегмент
    район×комнатность) и дают нестабильные, неправдоподобные веса на
    текущем объёме событий (см. docs/dom_forecast_audit.md §4)."""
    def seg_med(d, rb):
        return seg_median.get((d, rb), global_median)

    def feats(rec):
        return {
            "intercept": 1.0,
            "price_dev": math.log(rec["ppm2"]) - math.log(seg_med(rec["district"], rec["rooms"])),
            "log_area": math.log(rec["area"]),
        }

    names = list(feats(records[0]).keys())
    X = np.array([[feats(r)[k] for k in names] for r in records])
    T = np.array([r["T_train"] for r in records])
    E = np.array([r["event_train"] for r in records])
    logT = np.log(T)

    def neg_log_lik(params):
        beta, sigma = params[:-1], math.exp(params[-1])
        mu = X @ beta
        z = (logT - mu) / sigma
        ll_event = stats.norm.logpdf(z) - logT - math.log(sigma)
        ll_cens = stats.norm.logsf(z)
        return -np.sum(np.where(E == 1, ll_event, ll_cens))

    x0 = np.zeros(X.shape[1] + 1)
    x0[0] = logT[E == 1].mean() if E.sum() else logT.mean()
    x0[-1] = math.log(max(logT.std(), 0.1))
    res = optimize.minimize(neg_log_lik, x0, method="L-BFGS-B")
    return names, res.x[:-1], math.exp(res.x[-1]), res.success, feats


def _baseline_segment_median(records):
    seg_dom = defaultdict(list)
    for r in records:
        if r["event_train"] == 1:
            seg_dom[(r["district"], r["rooms"])].append(r["T_train"])
    global_dom = float(np.median([r["T_train"] for r in records if r["event_train"] == 1]))

    def predict(rec):
        v = seg_dom.get((rec["district"], rec["rooms"]))
        return float(np.median(v)) if v and len(v) >= MIN_SEGMENT_N else global_dom

    return predict


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

    seg_median, global_median = _segment_medians(records)
    names, beta, sigma, converged, feats = _fit_lean_aft(records, seg_median, global_median)
    print(f"\nAFT сошёлся: {converged}, sigma={sigma:.3f}")
    for n, b in zip(names, beta):
        print(f"  {n}: {b:+.4f}")

    def aft_pred(rec):
        x = np.array([feats(rec)[k] for k in names])
        return math.exp(x @ beta)

    baseline_pred = _baseline_segment_median(records)

    actuals = np.array([r["true_tom"] for r in test_recs])
    aft_preds = np.array([aft_pred(r) for r in test_recs])
    base_preds = np.array([baseline_pred(r) for r in test_recs])

    def mae(p):
        return float(np.mean(np.abs(p - actuals)))

    def medae(p):
        return float(np.median(np.abs(p - actuals)))

    print(f"\n=== BACKTEST (N={len(test_recs)}) ===")
    print(f"AFT (экономная):        MAE={mae(aft_preds):.1f} дн  medAE={medae(aft_preds):.1f} дн  "
          f"pred range=[{aft_preds.min():.1f}, {aft_preds.max():.1f}]")
    print(f"Baseline (сегм. медиана): MAE={mae(base_preds):.1f} дн  medAE={medae(base_preds):.1f} дн")
    verdict = "ПРОШЁЛ" if mae(aft_preds) < mae(base_preds) else "НЕ ПРОШЁЛ"
    print(f"\nQuality gate (AFT должен побить baseline): {verdict}")


if __name__ == "__main__":
    asyncio.run(main())
