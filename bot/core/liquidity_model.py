"""Модель ликвидности (Фаза D вердикт-стратегии, задача 2026-10-02).

Две outcome-модели LightGBM поверх признаков `liquidity_features`:

* exit14 — P(объявление пропадёт из выдачи Крыши в ближайшие 14 дней).
  Это НЕ вероятность продажи: ~30% уходов — перевыставления (релист под новым
  id), отличить продажу от снятия без данных о сделках нельзя.  Модель на
  «чистом» уходе (без релиста) гейт на temporal-holdout не прошла — в прод
  не идёт, см. docs/phase_c_liquidity_report.md.
* cut30 — P(продавец снизит цену в ближайшие 30 дней).

Гейт (§8 verdict_strategy.md): модель активируется, только если на
group-CV по listing_id нижняя граница bootstrap-CI разницы AUC
(модель − сырой price_score на t0) > 0.  Иначе остаётся предыдущая
активная модель (или прогноза нет вовсе).

score_total НЕ меняется (freeze-лист §6) — прогноз отдельный блок вердикта.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from bot.core import liquidity_features as lf

MODELS_DIR = Path(os.environ.get('LIQUIDITY_MODELS_DIR', Path(__file__).resolve().parents[2] / 'models' / 'liquidity'))
TARGETS = {'exit14': 14, 'cut30': 30}
LABEL_LAG_DAYS = 4          # last_seen должен «устояться»: круг sweep ≤ 2 дней + запас
TRAIN_T0_START = date(2026, 8, 15)
TRAIN_T0_STEP = 7
PARAMS = dict(objective='binary', learning_rate=0.05, num_leaves=31, min_child_samples=50, subsample=0.8,
              subsample_freq=1, colsample_bytree=0.8, reg_lambda=1.0, n_estimators=400, verbose=-1, n_jobs=4)

# Человеческие подписи признаков для объяснения прогноза в карточке
FEATURE_LABELS = {
    'dom': 'сколько дней объявление на рынке',
    'cuts_before': 'сколько раз цену уже снижали',
    'raises_before': 'повышения цены',
    'cum_change_pct': 'изменение цены с первой публикации',
    'days_since_cut': 'давность последнего снижения цены',
    'cuts_per_30d': 'частота снижений цены',
    'views_t0': 'число просмотров',
    'views_vel': 'просмотры за последнюю неделю',
    'views_per_dom': 'просмотров в день',
    'rdi': 'интерес покупателей vs похожие объявления (неделя)',
    'rdi_dom': 'интерес покупателей vs похожие объявления',
    'is_owner': 'собственник / посредник',
    'seller_type': 'тип продавца',
    'seller_active_t0': 'сколько объявлений у продавца',
    'seller_generic': 'продавец без имени',
    'is_urgent': 'пометка «срочно»',
    'ds_price': 'цена относительно аналогов',
    'ds_total': 'Deal Score',
    'ds_discount': 'дисконт к рыночной цене',
    'ds_analogs': 'число аналогов',
    'ds_conf': 'надёжность оценки цены',
    'ds_complete': 'полнота данных',
    'ppm2_vs_seg': 'цена м² vs соседние такие же квартиры',
    'price_pct_in_type': 'цена среди квартир того же типа',
    'log_price': 'цена',
    'ppm2': 'цена за м²',
    'comp_cell_rooms': 'конкуренция рядом (та же комнатность)',
    'comp_house': 'конкуренция в том же доме',
    'seg_share_new7': 'доля свежих объявлений рядом',
    'seg_med_dom': 'как долго висят соседние объявления',
    'type_share': 'массовость типа квартиры',
    'seg_med_ppm2': 'уровень цен в районе',
    'rooms': 'комнатность', 'area': 'площадь', 'floor': 'этаж', 'floors_total': 'этажность',
    'floor_ratio': 'положение этажа', 'is_first_floor': 'первый этаж', 'is_last_floor': 'последний этаж',
    'market_type': 'первичка / вторичка', 'is_new_build': 'новостройка',
    'cx_class': 'класс ЖК', 'cx_newbuild': 'ЖК-новостройка', 'cx_year': 'год постройки дома',
    'cx_parking': 'паркинг', 'cx_closed': 'закрытый двор', 'cx_under_umbrella': 'корпус большого ЖК',
    'dev_score': 'репутация застройщика', 'dev_court': 'суды застройщика', 'dev_delay': 'задержки застройщика',
    'loc_score': 'локация', 'loc_transport': 'транспорт', 'loc_infra': 'инфраструктура',
    'loc_noise': 'шум', 'loc_green': 'зелень', 'loc_risk': 'риски локации',
    'district': 'район', 'lat': 'расположение', 'lon': 'расположение',
}


def training_t0s(today: date, horizon_days: int) -> list[date]:
    """Еженедельные t0, для которых исход уже полностью наблюдаем."""
    last = today - timedelta(days=horizon_days + LABEL_LAG_DAYS)
    out, t = [], TRAIN_T0_START
    while t <= last:
        out.append(t)
        t += timedelta(days=TRAIN_T0_STEP)
    return out


def _git_commit() -> str | None:
    try:
        return subprocess.check_output(['git', 'rev-parse', '--short', 'HEAD'],
                                       cwd=Path(__file__).resolve().parents[2], text=True).strip()
    except Exception:
        return None


def _auc(y, p) -> float:
    from sklearn.metrics import roc_auc_score  # noqa: WPS433 — sklearn только в обучении
    return float(roc_auc_score(y, p))


def _bootstrap_diff(y, a, b, n=300, seed=0) -> tuple[float, float]:
    from sklearn.metrics import roc_auc_score
    rng = np.random.default_rng(seed)
    d = []
    for _ in range(n):
        s = rng.integers(0, len(y), len(y))
        if y[s].min() == y[s].max():
            continue
        d.append(roc_auc_score(y[s], a[s]) - roc_auc_score(y[s], b[s]))
    lo, hi = np.percentile(d, [2.5, 97.5])
    return float(lo), float(hi)


def category_levels(df: pd.DataFrame) -> dict[str, list]:
    return {c: sorted(df[c].dropna().astype(str).unique().tolist()) for c in lf.CATEGORICAL}


def evaluate_and_fit(df: pd.DataFrame, target: str) -> tuple[object, dict]:
    """Group-CV по listing_id (OOF AUC vs сырой price_score на тех же строках) + финальная модель."""
    import lightgbm as lgb
    from sklearn.model_selection import StratifiedGroupKFold
    df = df.reset_index(drop=True)
    levels = category_levels(df)
    X = lf.to_model_matrix(df, levels)
    y = df[target].to_numpy()
    base = df.ds_price.fillna(df.ds_price.median()).to_numpy()
    oof = np.zeros(len(df))
    for tr, te in StratifiedGroupKFold(5, shuffle=True, random_state=42).split(X, y, df.listing_id):
        m = lgb.LGBMClassifier(**PARAMS).fit(X.iloc[tr], y[tr])
        oof[te] = m.predict_proba(X.iloc[te])[:, 1]
    lo, hi = _bootstrap_diff(y, oof, base)
    metrics = {
        'target': target, 'n': int(len(df)), 'pos_rate': float(y.mean()),
        't0s': sorted(df.t0.unique().tolist()),
        'auc_cv': _auc(y, oof), 'auc_baseline_price_score': _auc(y, base),
        'auc_diff_ci95': [lo, hi], 'gate_passed': bool(lo > 0),
        'auc_cv_by_t0': {t: _auc(y[(df.t0 == t).to_numpy()], oof[(df.t0 == t).to_numpy()])
                          for t in sorted(df.t0.unique())},
    }
    model = lgb.LGBMClassifier(**PARAMS).fit(X, y)
    return model, {'metrics': metrics, 'categories': levels}


@dataclass
class ActiveModel:
    target: str
    version: str
    booster: object
    categories: dict
    meta: dict


def save_model(target: str, model, info: dict, *, activate: bool) -> str:
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    version = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')
    stem = MODELS_DIR / f'{target}_{version}'
    model.booster_.save_model(str(stem) + '.txt')
    meta = {**info, 'target': target, 'version': version, 'git_commit': _git_commit(),
            'features': lf.FEATURES, 'trained_at': datetime.now(timezone.utc).isoformat()}
    (Path(str(stem) + '.json')).write_text(json.dumps(meta, ensure_ascii=False, indent=1, default=float))
    if activate:
        (MODELS_DIR / f'{target}_active.json').write_text(json.dumps({'version': version}))
    return version


def load_active(target: str) -> ActiveModel | None:
    import lightgbm as lgb
    ptr = MODELS_DIR / f'{target}_active.json'
    if not ptr.exists():
        return None
    version = json.loads(ptr.read_text())['version']
    meta = json.loads((MODELS_DIR / f'{target}_{version}.json').read_text())
    booster = lgb.Booster(model_file=str(MODELS_DIR / f'{target}_{version}.txt'))
    return ActiveModel(target, version, booster, meta['categories'], meta)


def explain(contrib_row: np.ndarray, x_row: pd.Series, top: int = 3) -> list[dict]:
    """Топ-вклады признаков (SHAP из LightGBM pred_contrib) в человекочитаемом виде.
    Признаки lat/lon склеиваются в одно «расположение»."""
    contrib = pd.Series(contrib_row[:-1], index=lf.FEATURES)   # последний столбец — bias
    merged: dict[str, float] = {}
    for f, v in contrib.items():
        label = FEATURE_LABELS.get(f, f)
        merged[label] = merged.get(label, 0.0) + float(v)
    feat_by_label = {}
    for f in lf.FEATURES:
        feat_by_label.setdefault(FEATURE_LABELS.get(f, f), f)
    items = sorted(merged.items(), key=lambda kv: -abs(kv[1]))
    out = []
    for label, v in items:
        if abs(v) < 0.02 or len(out) >= top:
            break
        f = feat_by_label[label]
        val = x_row.get(f)
        out.append({'feature': f, 'label': label, 'effect': 'up' if v > 0 else 'down',
                    'weight': round(v, 3),
                    'value': None if val is None or (isinstance(val, float) and np.isnan(val)) else
                    (round(float(val), 2) if isinstance(val, (int, float, np.floating)) else str(val))})
    return out


def predict_frame(df: pd.DataFrame, am: ActiveModel) -> tuple[np.ndarray, np.ndarray, pd.DataFrame]:
    X = lf.to_model_matrix(df, am.categories)
    p = am.booster.predict(X)
    contrib = am.booster.predict(X, pred_contrib=True)
    return p, contrib, X


def percentile_rank(p: np.ndarray) -> np.ndarray:
    """0..100: доля объявлений сегодняшней популяции с меньшей вероятностью."""
    return pd.Series(p).rank(pct=True, method='average').mul(100).round(1).to_numpy()
