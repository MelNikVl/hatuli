"""Модель ликвидности (Фаза C/D, задача 2026-10-02): признаки, гейт-инфраструктура,
объяснения. Без БД — синтетические «сырые» выборки той же формы, что load_raw()."""
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from bot.core import liquidity_features as lf
from bot.core import liquidity_model as lm

T0 = date(2026, 8, 22)


def _raw(n=60, seed=0):
    rng = np.random.default_rng(seed)
    t0dt = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)
    base = pd.DataFrame({
        'listing_id': [str(1000 + i) for i in range(n)],
        'first_seen': [t0dt - timedelta(days=int(d)) for d in rng.integers(0, 90, n)],
        'last_seen': [t0dt + timedelta(days=int(d)) for d in rng.integers(0, 40, n)],
        'rooms': rng.integers(1, 5, n), 'area': rng.uniform(30, 120, n),
        'floor': rng.integers(1, 12, n), 'floors_total': 12,
        'market_type': rng.choice(['secondary', 'primary'], n), 'is_new_build': rng.choice([True, False], n),
        'seller_type': rng.choice(['owner', 'realtor', None], n), 'is_owner': rng.choice([True, False], n),
        'is_urgent': False, 'district': rng.choice(['Есиль', 'Алматы'], n),
        'lat': rng.uniform(51.08, 51.2, n), 'lon': rng.uniform(71.35, 71.5, n),
        'resolved_house_id': rng.choice([1, 2, None], n),
        'seller_norm': rng.choice(['хозяин', 'асель', 'ultra realty', ''], n),
        'price_t0': rng.integers(15, 79, n) * 1_000_000, 'views_t0': rng.integers(10, 500, n).astype(float),
        'views_tk': rng.integers(0, 10, n).astype(float),
    })
    ds = pd.DataFrame({'listing_id': base.listing_id, 'ds_total': rng.integers(20, 90, n),
                       'ds_price': rng.integers(0, 100, n), 'ds_discount': rng.normal(0, 5, n),
                       'ds_analogs': rng.integers(0, 30, n), 'ds_conf': 50, 'ds_complete': 70})
    ph = pd.DataFrame({'listing_id': base.listing_id[:10], 'cuts_before': 1, 'raises_before': 0,
                       'last_cut_at': t0dt - timedelta(days=3), 'first_price': base.price_t0[:10] * 1.1,
                       'cut30': [True, False] * 5})
    cx = pd.DataFrame({'resolved_house_id': [1, 2], 'cx_class': ['комфорт', 'бизнес'], 'cx_newbuild': [True, False],
                       'cx_year': [2020, 2008], 'cx_parking': [True, None], 'cx_closed': [False, True],
                       'cx_under_umbrella': [False, False], 'dev_score': [70, None], 'dev_court': [False, None],
                       'dev_delay': [0, None]})
    loc = pd.DataFrame({'resolved_house_id': [1], 'loc_score': [66], 'loc_transport': [70], 'loc_infra': [60],
                        'loc_noise': [50], 'loc_green': [40], 'loc_risk': [90]})
    return {'base': base, 'ds': ds, 'ph': ph, 'cx': cx, 'loc': loc}


def test_engineer_produces_all_features():
    df = lf.engineer(_raw(), T0)
    missing = [f for f in lf.FEATURES if f not in df.columns]
    assert not missing
    assert len(df) == 60


def test_no_label_or_future_columns_in_features():
    future = {'last_seen', 'exit14', 'exit30', 'cut30', 'archived_at', 'is_active'}
    assert not future & set(lf.FEATURES)


@pytest.mark.parametrize('col', ['year_built', 'building_type', 'renovation', 'finish_level',
                                 'ceiling_height', 'kitchen_area', 'description', 'photos', 'floorplan_url'])
def test_backfilled_card_fields_not_used(col):
    """Поля, которые парсер дописывает после t0 (утечка, отчёт Фазы C) — не в признаках и не в SQL."""
    assert col not in lf.FEATURES
    assert f'a.{col}' not in lf.BASE_SQL


def test_dom_and_price_cut_features():
    raw = _raw()
    df = lf.engineer(raw, T0).set_index('listing_id')
    lid = raw['base'].listing_id[0]
    assert df.loc[lid, 'cuts_before'] == 1
    assert df.loc[lid, 'cum_change_pct'] == pytest.approx((1 / 1.1 - 1) * 100)
    assert df.loc[raw['base'].listing_id[20], 'cuts_before'] == 0      # нет событий — 0, не NaN
    assert (df.dom > 0).all()


def test_views_velocity_uses_lag():
    raw = _raw()
    df = lf.engineer(raw, T0)
    k = lf.views_lag_days(T0)
    assert k == 7
    exp = (raw['base'].views_t0 - raw['base'].views_tk) / k
    assert np.allclose(df.views_vel.to_numpy(), exp.to_numpy())


def test_generic_seller_not_aggregated():
    df = lf.engineer(_raw(), T0)
    gen = df.seller_norm.isin(lf.GENERIC_SELLERS) | (df.seller_norm == '')
    assert df.loc[gen, 'seller_active_t0'].isna().all()
    assert (df.loc[gen, 'seller_generic'] == 1).all()


def test_labels_from_last_seen():
    df = lf.add_labels(lf.engineer(_raw(), T0), T0)
    t0ts = pd.Timestamp(T0, tz=lf.TZ)
    ls = pd.to_datetime(df.last_seen, utc=True).dt.tz_convert(lf.TZ)
    assert (df.exit14 == (ls < t0ts + pd.Timedelta(days=14)).astype(int)).all()
    assert set(df.cut30.unique()) <= {0, 1}


def test_legacy_coverage_window():
    assert lf.legacy_coverage_applies(date(2026, 8, 22), 30)
    assert not lf.legacy_coverage_applies(date(2026, 9, 20), 14)


def test_training_t0s_only_observed_windows():
    t0s = lm.training_t0s(date(2026, 10, 2), 14)
    assert t0s[0] == lm.TRAIN_T0_START
    assert all(t + timedelta(days=14 + lm.LABEL_LAG_DAYS) <= date(2026, 10, 2) for t in t0s)
    assert all((b - a).days == 7 for a, b in zip(t0s, t0s[1:]))


def test_model_matrix_categories_stable_between_train_and_serve():
    df = lf.engineer(_raw(), T0)
    levels = lm.category_levels(df)
    X1 = lf.to_model_matrix(df, levels)
    X2 = lf.to_model_matrix(df.iloc[:5], levels)       # serve: подвыборка с частью уровней
    for c in lf.CATEGORICAL:
        assert list(X1[c].cat.categories) == list(X2[c].cat.categories)
    assert list(X1.columns) == lf.FEATURES


def test_train_save_load_predict_explain(tmp_path, monkeypatch):
    lgb = pytest.importorskip('lightgbm')
    monkeypatch.setattr(lm, 'MODELS_DIR', tmp_path)
    df = lf.add_labels(lf.engineer(_raw(n=400, seed=1), T0), T0)
    levels = lm.category_levels(df)
    params = {**lm.PARAMS, 'n_estimators': 20, 'min_child_samples': 5}
    model = lgb.LGBMClassifier(**params).fit(lf.to_model_matrix(df, levels), df.exit14)
    ver = lm.save_model('exit14', model, {'metrics': {}, 'categories': levels}, activate=True)
    am = lm.load_active('exit14')
    assert am is not None and am.version == ver
    p, contrib, X = lm.predict_frame(df, am)
    assert p.shape == (len(df),) and ((p >= 0) & (p <= 1)).all()
    assert contrib.shape == (len(df), len(lf.FEATURES) + 1)
    # SHAP-аддитивность: сумма вкладов = логит
    assert np.allclose(contrib.sum(axis=1), np.log(p / (1 - p)), atol=1e-6)
    reasons = lm.explain(contrib[0], X.iloc[0])
    assert len(reasons) <= 3
    assert all(r['effect'] in ('up', 'down') for r in reasons)
    assert sum(r['label'] == 'расположение' for r in reasons) <= 1   # lat/lon склеены


def test_inactive_gate_keeps_previous(tmp_path, monkeypatch):
    lgb = pytest.importorskip('lightgbm')
    monkeypatch.setattr(lm, 'MODELS_DIR', tmp_path)
    df = lf.add_labels(lf.engineer(_raw(n=200, seed=2), T0), T0)
    levels = lm.category_levels(df)
    m = lgb.LGBMClassifier(**{**lm.PARAMS, 'n_estimators': 5, 'min_child_samples': 5}).fit(
        lf.to_model_matrix(df, levels), df.exit14)
    v1 = lm.save_model('cut30', m, {'metrics': {}, 'categories': levels}, activate=True)
    import time; time.sleep(1.1)
    lm.save_model('cut30', m, {'metrics': {}, 'categories': levels}, activate=False)
    assert lm.load_active('cut30').version == v1


def test_percentile_rank_bounds():
    r = lm.percentile_rank(np.array([0.1, 0.5, 0.9]))
    assert r.min() > 0 and r.max() == 100
    assert list(np.argsort(r)) == [0, 1, 2]
