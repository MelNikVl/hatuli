#!/usr/bin/env python3
"""Фаза C: ablation study (year_built объявления исключён: его дозаполняет coord_backfill ПОСЛЕ t0 — утечка)
 — блоки признаков по одному, честный AUC.

Протокол:
  1) CV: StratifiedGroupKFold(5) по listing_id на объединённых t0 (одно объявление
     целиком в одном фолде) — OOF-прогнозы, AUC по каждому t0.
  2) Temporal: обучение на t0=A, тест на t0=B только по НОВЫМ объявлениям
     (first_seen > A) — модель их не видела ни в каком виде.
  Baseline — сырой price_score из deal_score_snapshots на t0 (как он был посчитан
  в проде в тот день), без обучения.  Bootstrap CI (paired) на разницу AUC.
"""
import sys, json, warnings
import numpy as np, pandas as pd, lightgbm as lgb
from sklearn.model_selection import StratifiedGroupKFold
from sklearn.metrics import roc_auc_score, average_precision_score
warnings.filterwarnings('ignore')

BLOCKS = [
 ('price', ['ds_price','ds_total','ds_discount','ds_analogs','ds_conf','ds_complete','ppm2_vs_seg','price_pct_in_type','log_price','ppm2']),
 ('dom', ['dom','cuts_before','raises_before','cum_change_pct','days_since_cut','cuts_per_30d']),
 ('views', ['views_t0','views_vel','views_per_dom','rdi','rdi_dom']),
 ('seller', ['is_owner','seller_type','seller_active_t0','seller_generic','is_urgent']),
 ('competition', ['comp_cell_rooms','comp_house','seg_share_new7','seg_med_dom','type_share','seg_med_ppm2']),
 # Поля карточки, которые парсер ДОЗАПОЛНЯЕТ/ПЕРЕЗАПИСЫВАЕТ после t0 (detail re-fetch при смене цены,
 # coord_backfill, бэкфиллы) — исключены: их наличие/значение зависит от будущего (утечка, проверено по
 # разнице null-rate между классами): year_built, building_type, renovation, finish_level, ceiling_height,
 # kitchen_area, desc_len, n_photos, has_floorplan.
 ('property', ['rooms','area','floor','floors_total','floor_ratio','is_first_floor','is_last_floor',
               'market_type','is_new_build','cx_class','cx_newbuild','cx_year','cx_parking','cx_closed',
               'cx_under_umbrella','dev_score','dev_court','dev_delay']),
 ('location', ['loc_score','loc_transport','loc_infra','loc_noise','loc_green','loc_risk','district','lat','lon']),
]
CATS = ['seller_type','building_type','renovation','finish_level','market_type','cx_class','district','ds_method']
PARAMS = dict(objective='binary', learning_rate=0.05, num_leaves=31, min_child_samples=50, subsample=0.8,
              subsample_freq=1, colsample_bytree=0.8, reg_lambda=1.0, n_estimators=400, verbose=-1, n_jobs=8)

def prep(df):
    df = df.copy()
    for c in CATS:
        if c in df: df[c] = df[c].astype('category')
    for c in df.columns:
        if df[c].dtype == object and c not in ('listing_id','t0','first_seen','last_seen','seller_norm','cell','first_cut_at','last_cut_at'):
            df[c] = pd.to_numeric(df[c], errors='coerce')
        if df[c].dtype == bool: df[c] = df[c].astype(float)
    return df

def boot_diff(y, a, b, n=300, seed=0):
    rng = np.random.default_rng(seed); idx = np.arange(len(y)); d = []
    for _ in range(n):
        s = rng.choice(idx, len(idx))
        if y[s].min() == y[s].max(): continue
        d.append(roc_auc_score(y[s], a[s]) - roc_auc_score(y[s], b[s]))
    return np.percentile(d, [2.5, 97.5])

def run(df, target, t0s):
    df = prep(df[df.t0.isin(t0s)]).reset_index(drop=True)
    y = df[target].values
    base = df.ds_price.fillna(df.ds_price.median()).values
    res = {'target': target, 't0s': t0s, 'n': len(df), 'pos_rate': float(y.mean()), 'cv': [], 'temporal': []}
    # baseline per t0
    print(f'\n=== {target}  t0={t0s}  n={len(df)}  pos={y.mean():.3f}')
    for t in t0s:
        m = (df.t0 == t).values
        print(f'  baseline price_score  t0={t}: AUC={roc_auc_score(y[m], base[m]):.4f}  score_total={roc_auc_score(y[m], df.ds_total.fillna(50).values[m]):.4f}')
    sgk = StratifiedGroupKFold(n_splits=5, shuffle=True, random_state=42)
    folds = list(sgk.split(df, y, groups=df.listing_id))
    feats = []; prev = None
    for name, cols in BLOCKS:
        feats = feats + [c for c in cols if c in df.columns]
        oof = np.zeros(len(df))
        for tr, te in folds:
            m = lgb.LGBMClassifier(**PARAMS).fit(df.loc[tr, feats], y[tr])
            oof[te] = m.predict_proba(df.loc[te, feats])[:, 1]
        row = {'block': '+' + name, 'n_feats': len(feats), 'auc_all': roc_auc_score(y, oof), 'prauc_all': average_precision_score(y, oof)}
        for t in t0s:
            mk = (df.t0 == t).values
            row[f'auc_{t}'] = roc_auc_score(y[mk], oof[mk])
        lo, hi = boot_diff(y, oof, base)
        row['d_vs_base_ci'] = [round(lo, 4), round(hi, 4)]
        if prev is not None:
            lo2, hi2 = boot_diff(y, oof, prev, seed=1); row['d_vs_prev_ci'] = [round(lo2, 4), round(hi2, 4)]
        prev = oof
        res['cv'].append(row)
        print('  CV', json.dumps({k: (round(v, 4) if isinstance(v, float) else v) for k, v in row.items()}, ensure_ascii=False))
    # temporal: train first t0, test later t0 new listings only
    tr_t = t0s[0]; tr = (df.t0 == tr_t).values
    for te_t in t0s[1:]:
        te = ((df.t0 == te_t) & (pd.to_datetime(df.first_seen, utc=True) > pd.Timestamp(tr_t, tz='Asia/Almaty') + pd.Timedelta(days=1))).values
        feats = []; out = {'train': tr_t, 'test': te_t, 'n_test': int(te.sum()), 'base': roc_auc_score(y[te], base[te])}
        for name, cols in BLOCKS:
            feats = feats + [c for c in cols if c in df.columns]
            m = lgb.LGBMClassifier(**PARAMS).fit(df.loc[tr, feats], y[tr])
            out['+' + name] = roc_auc_score(y[te], m.predict_proba(df.loc[te, feats])[:, 1])
        res['temporal'].append(out)
        print('  TEMPORAL', json.dumps({k: (round(v, 4) if isinstance(v, float) else v) for k, v in out.items()}))
    return res

if __name__ == '__main__':
    df = pd.read_pickle(sys.argv[1])
    T3 = ['2026-08-15', '2026-08-22', '2026-08-29']; T2 = T3[:2]
    allres = [run(df, 'exit14', T3), run(df, 'exit14_clean', T3), run(df, 'cut30', T2)]
    json.dump(allres, open(sys.argv[2], 'w'), ensure_ascii=False, indent=1, default=float)
