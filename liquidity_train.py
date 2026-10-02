#!/usr/bin/env python3
"""Еженедельное переобучение моделей ликвидности (Фаза D, задача 2026-10-02).

Для каждой цели (exit14, cut30): собирает признаки на еженедельных t0, для
которых исход уже наблюдаем, меряет group-CV AUC против сырого price_score
и активирует новую модель ТОЛЬКО если гейт пройден (нижняя граница CI > 0).
Иначе модель сохраняется для истории, но активной остаётся предыдущая.

  venv/bin/python liquidity_train.py            # обычный запуск (таймер)
  venv/bin/python liquidity_train.py --dry-run  # только метрики, без сохранения
"""
import argparse
import json
import logging
import os
import sys
from datetime import date

import pandas as pd
import psycopg2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bot.core import liquidity_features as lf  # noqa: E402
from bot.core import liquidity_model as lm  # noqa: E402

log = logging.getLogger('liquidity_train')


def _db_url() -> str:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))
    return os.environ['DATABASE_URL']


def build_training_set(conn, target: str, today: date) -> pd.DataFrame:
    horizon = lm.TARGETS[target]
    parts = []
    for t0 in lm.training_t0s(today, horizon):
        raw = lf.load_raw(conn, t0, mode='train', horizon_days=horizon)
        if raw['base'].empty:
            continue
        df = lf.add_labels(lf.engineer(raw, t0), t0)
        parts.append(df)
        log.info('%s t0=%s n=%d pos=%.3f', target, t0, len(df), df[target].mean())
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--today', type=date.fromisoformat, default=date.today())
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    conn = psycopg2.connect(_db_url())
    rc = 0
    for target in lm.TARGETS:
        df = build_training_set(conn, target, args.today)
        if df.empty:
            log.warning('%s: нет t0 с наблюдаемым исходом — пропуск', target)
            continue
        model, info = lm.evaluate_and_fit(df, target)
        m = info['metrics']
        log.info('%s: AUC cv=%.4f baseline(price_score)=%.4f diff CI95=[%.4f, %.4f] gate=%s',
                 target, m['auc_cv'], m['auc_baseline_price_score'], *m['auc_diff_ci95'], m['gate_passed'])
        print(json.dumps(m, ensure_ascii=False, default=float))
        if args.dry_run:
            continue
        version = lm.save_model(target, model, info, activate=m['gate_passed'])
        log.info('%s: сохранена версия %s (%s)', target, version,
                 'АКТИВНА' if m['gate_passed'] else 'гейт не пройден — активной осталась прежняя')
        if not m['gate_passed']:
            rc = 2
    return rc


if __name__ == '__main__':
    sys.exit(main())
