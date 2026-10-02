#!/usr/bin/env python3
"""Ежедневный прогноз ликвидности для живых объявлений (Фаза D, задача 2026-10-02).

Запускается после krisha-listing-snapshot/deal-score-snapshot (нужны снимки
за сегодня). Пишет liquidity_predictions(as_of=сегодня). Повторный запуск в
тот же день перезаписывает строки этого дня (ON CONFLICT DO UPDATE).
"""
import argparse
import json
import logging
import os
import sys
from datetime import date

import numpy as np
import psycopg2
from psycopg2.extras import execute_values

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bot.core import liquidity_features as lf  # noqa: E402
from bot.core import liquidity_model as lm  # noqa: E402

log = logging.getLogger('liquidity_predict')


def _db_url() -> str:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))
    return os.environ['DATABASE_URL']


def _f(x):
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else float(x)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--as-of', type=date.fromisoformat, default=date.today())
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    models = {t: lm.load_active(t) for t in lm.TARGETS}
    models = {t: m for t, m in models.items() if m is not None}
    if not models:
        log.error('нет активных моделей — сначала liquidity_train.py')
        return 1
    conn = psycopg2.connect(_db_url())
    raw = lf.load_raw(conn, args.as_of, mode='live')
    if raw['base'].empty:
        log.error('нет listing_snapshots за %s — снапшот ещё не прогнан?', args.as_of)
        return 1
    df = lf.engineer(raw, args.as_of).reset_index(drop=True)
    out = {t: {} for t in models}
    for t, am in models.items():
        p, contrib, X = lm.predict_frame(df, am)
        out[t] = {'p': p, 'pct': lm.percentile_rank(p),
                  'reasons': [lm.explain(contrib[i], X.iloc[i]) for i in range(len(df))]}
        log.info('%s v%s: n=%d mean p=%.3f', t, am.version, len(df), float(p.mean()))
    versions = {t: am.version for t, am in models.items()}
    commit = lm._git_commit()
    rows = []
    for i, lid in enumerate(df.listing_id):
        g = lambda t, k: (out[t][k][i] if t in out else None)  # noqa: E731
        rows.append((lid, args.as_of, _f(g('exit14', 'p')), _f(g('exit14', 'pct')),
                     _f(g('cut30', 'p')), _f(g('cut30', 'pct')),
                     json.dumps({t: out[t]['reasons'][i] for t in out}, ensure_ascii=False),
                     json.dumps(versions), commit))
    with conn, conn.cursor() as cur:
        execute_values(cur, """
          INSERT INTO liquidity_predictions
            (listing_id, as_of, p_exit14, exit14_pct, p_cut30, cut30_pct, reasons, model_versions, git_commit)
          VALUES %s
          ON CONFLICT (listing_id, as_of) DO UPDATE SET
            p_exit14=EXCLUDED.p_exit14, exit14_pct=EXCLUDED.exit14_pct, p_cut30=EXCLUDED.p_cut30,
            cut30_pct=EXCLUDED.cut30_pct, reasons=EXCLUDED.reasons, model_versions=EXCLUDED.model_versions,
            git_commit=EXCLUDED.git_commit, computed_at=now()""", rows, page_size=2000)
    log.info('записано %d прогнозов за %s', len(rows), args.as_of)
    return 0


if __name__ == '__main__':
    sys.exit(main())
