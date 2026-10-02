"""Признаки модели ликвидности (Фаза C/D вердикт-стратегии, задача 2026-10-02).

Один и тот же код строит признаки для обучения (исторические t0) и для
ежедневного прогноза (t0 = сегодня) — чтобы train/serve не разъехались.

Ключевые решения (полный отчёт — docs/phase_c_liquidity_report.md):

* Исход — по `last_seen`, НЕ по `archived_at`/`is_active`. Archive-check
  проверяет малую долю объявлений, `is_active=TRUE` стоит у ~107K строк при
  ~19K живых на Крыше; метка на `archived_at` систематически недосчитывает
  уходы. Deep sweep видит каждое живое объявление раз в ~1.5-2 дня, поэтому
  «last_seen < t0+H» = «пропало из выдачи в течение H дней».
* До 2026-09-28 поиск шёл с потолком 80 млн и только с фото — для остальных
  объявлений last_seen не обновлялся, метка была бы ложной. Для окон,
  захватывающих этот период, популяция ограничена покрытым сегментом
  (`legacy_coverage_applies`).
* Признаки — только то, что известно на t0: listing_snapshots (цена/просмотры
  по дням), deal_score_snapshots (скор как он был посчитан в тот день),
  price_history до t0, first_seen, статические атрибуты, которые парсер не
  дописывает задним числом.
* Исключены поля карточки, которые парсер ДОЗАПОЛНЯЕТ после t0 (detail
  re-fetch при смене цены, coord_backfill): year_built, building_type,
  renovation, finish_level, ceiling_height, kitchen_area, длина описания,
  число фото, планировка. Их наличие зависит от будущего — проверено по
  разнице null-rate между классами исхода (kitchen_area давал +0.06 AUC на
  cut30 исключительно через утечку).
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

VIEWS_START = date(2026, 8, 14)          # старт listing_snapshots.views
COVERAGE_FIX_DATE = date(2026, 9, 28)    # снят потолок 80 млн и фильтр «с фото»
LEGACY_MAX_PRICE = 80_000_000
GENERIC_SELLERS = frozenset({
    'хозяин', 'хозяйка', 'продавец', 'собственник', 'владелец', 'агент',
    'риелтор', 'риэлтор', 'менеджер', 'отдел продаж',
})

FEATURE_BLOCKS: list[tuple[str, list[str]]] = [
    ('price', ['ds_price', 'ds_total', 'ds_discount', 'ds_analogs', 'ds_conf', 'ds_complete',
               'ppm2_vs_seg', 'price_pct_in_type', 'log_price', 'ppm2']),
    ('dom', ['dom', 'cuts_before', 'raises_before', 'cum_change_pct', 'days_since_cut', 'cuts_per_30d']),
    ('views', ['views_t0', 'views_vel', 'views_per_dom', 'rdi', 'rdi_dom']),
    ('seller', ['is_owner', 'seller_type', 'seller_active_t0', 'seller_generic', 'is_urgent']),
    ('competition', ['comp_cell_rooms', 'comp_house', 'seg_share_new7', 'seg_med_dom', 'type_share', 'seg_med_ppm2']),
    ('property', ['rooms', 'area', 'floor', 'floors_total', 'floor_ratio', 'is_first_floor', 'is_last_floor',
                  'market_type', 'is_new_build', 'cx_class', 'cx_newbuild', 'cx_year', 'cx_parking', 'cx_closed',
                  'cx_under_umbrella', 'dev_score', 'dev_court', 'dev_delay']),
    ('location', ['loc_score', 'loc_transport', 'loc_infra', 'loc_noise', 'loc_green', 'loc_risk',
                  'district', 'lat', 'lon']),
]
FEATURES: list[str] = [c for _, cols in FEATURE_BLOCKS for c in cols]
CATEGORICAL = ['seller_type', 'market_type', 'cx_class', 'district']
TZ = 'Asia/Almaty'


def legacy_coverage_applies(t0: date, horizon_days: int) -> bool:
    """Окно [t0, t0+H] захватывает период до снятия фильтров поиска."""
    return t0 + timedelta(days=horizon_days) < COVERAGE_FIX_DATE


def views_lag_days(t0: date) -> int:
    return max(1, min(7, (t0 - VIEWS_START).days))


# ---------------------------------------------------------------- SQL
BASE_SQL = """
  SELECT a.id AS listing_id, a.first_seen, a.last_seen, a.rooms, a.area, a.floor, a.floors_total,
         a.market_type, a.is_new_build, a.seller_type, a.is_owner, a.is_urgent, a.district,
         a.lat, a.lon, a.resolved_house_id,
         lower(regexp_replace(trim(coalesce(a.seller_name,'')), '\\s+', ' ', 'g')) AS seller_norm,
         s.price AS price_t0, s.views AS views_t0, sk.views AS views_tk
  FROM apartment_listings a
  JOIN listing_snapshots s ON s.listing_id = a.id AND s.date = %(t0)s
  LEFT JOIN listing_snapshots sk ON sk.listing_id = a.id AND sk.date = %(tk)s
  WHERE coalesce(a.is_duplicate, false) = false
    AND a.first_seen::date <= %(t0)s
    AND a.last_seen >= %(min_last_seen)s
    {coverage}
"""
COVERAGE_SQL = "AND s.price <= %(max_price)s AND a.photo_url IS NOT NULL"

DS_SQL = """
  SELECT DISTINCT ON (listing_id) listing_id, score_total AS ds_total, price_score AS ds_price,
         bargain_discount_pct AS ds_discount, bargain_analogs_count AS ds_analogs,
         deal_confidence AS ds_conf, data_completeness AS ds_complete
  FROM deal_score_snapshots WHERE observed_at::date = %(t0)s AND listing_id = ANY(%(ids)s)
  ORDER BY listing_id, observed_at DESC"""

PH_SQL = """
  SELECT listing_id,
    count(*) FILTER (WHERE changed_at::date <= %(t0)s AND new_price < old_price) AS cuts_before,
    count(*) FILTER (WHERE changed_at::date <= %(t0)s AND new_price > old_price) AS raises_before,
    max(changed_at) FILTER (WHERE changed_at::date <= %(t0)s AND new_price < old_price) AS last_cut_at,
    (array_agg(old_price ORDER BY changed_at) FILTER (WHERE changed_at::date <= %(t0)s))[1] AS first_price,
    bool_or(changed_at::date > %(t0)s AND changed_at::date <= %(t30)s AND new_price < old_price) AS cut30
  FROM price_history WHERE listing_id = ANY(%(ids)s) GROUP BY listing_id"""

CX_SQL = """
  SELECT c.id AS resolved_house_id, coalesce(c.housing_class, c.predicted_housing_class) AS cx_class,
         c.is_newbuild AS cx_newbuild, c.year_built AS cx_year, c.has_parking AS cx_parking,
         c.has_closed_territory AS cx_closed, c.parent_complex_id IS NOT NULL AS cx_under_umbrella,
         d.score_total AS dev_score, d.has_court_cases AS dev_court, d.avg_delay_months AS dev_delay
  FROM complexes c LEFT JOIN developers d ON d.id = c.developer_id"""

# Первый расчёт локации (score_version/набор источников мог меняться позже; берём
# самый ранний — он ближе к «известному на t0» для исторических t0).
LOC_SQL = """
  SELECT DISTINCT ON (complex_id) complex_id AS resolved_house_id, score AS loc_score,
         transport_score AS loc_transport, infra_score AS loc_infra, noise_score AS loc_noise,
         green_score AS loc_green, risk_score AS loc_risk
  FROM complex_location_scores ORDER BY complex_id, computed_at"""


def _read(conn, sql: str, params: dict | None = None) -> pd.DataFrame:
    with conn.cursor() as cur:
        cur.execute(sql, params or {})
        cols = [d[0] for d in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=cols)


def load_raw(conn, t0: date, *, mode: str, horizon_days: int = 14) -> dict[str, pd.DataFrame]:
    """mode='train': популяция — живые на t0 (last_seen >= t0, т.е. видели на/после t0).
    mode='live' (t0 = сегодня): last_seen >= t0-3д — объявление в текущем круге sweep."""
    assert mode in ('train', 'live')
    min_ls = t0 if mode == 'train' else t0 - timedelta(days=3)
    coverage = COVERAGE_SQL if (mode == 'train' and legacy_coverage_applies(t0, horizon_days)) else ''
    params = {'t0': t0, 'tk': t0 - timedelta(days=views_lag_days(t0)), 'min_last_seen': min_ls,
              'max_price': LEGACY_MAX_PRICE, 't30': t0 + timedelta(days=30)}
    base = _read(conn, BASE_SQL.format(coverage=coverage), params)
    params['ids'] = list(base.listing_id)
    return {'base': base, 'ds': _read(conn, DS_SQL, params), 'ph': _read(conn, PH_SQL, params),
            'cx': _read(conn, CX_SQL), 'loc': _read(conn, LOC_SQL)}


# ---------------------------------------------------------------- engineering
def _num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors='coerce').astype(float)


def engineer(raw: dict[str, pd.DataFrame], t0: date) -> pd.DataFrame:
    """Чистая функция: сырые выборки -> признаки. Без доступа к БД (тестируется на синтетике)."""
    df = (raw['base'].merge(raw['ds'], on='listing_id', how='left')
          .merge(raw['ph'], on='listing_id', how='left')
          .merge(raw['cx'], on='resolved_house_id', how='left')
          .merge(raw['loc'], on='resolved_house_id', how='left'))
    for c in ['price_t0', 'views_t0', 'views_tk', 'area', 'rooms', 'floor', 'floors_total', 'lat', 'lon',
              'ds_total', 'ds_price', 'ds_discount', 'ds_analogs', 'ds_conf', 'ds_complete', 'cuts_before',
              'raises_before', 'first_price', 'cx_year', 'dev_score', 'dev_delay', 'loc_score', 'loc_transport',
              'loc_infra', 'loc_noise', 'loc_green', 'loc_risk']:
        if c in df:
            df[c] = _num(df[c])
    for c in ['is_owner', 'is_urgent', 'is_new_build', 'cx_newbuild', 'cx_parking', 'cx_closed',
              'cx_under_umbrella', 'dev_court', 'cut30']:
        if c in df:
            df[c] = df[c].map({True: 1.0, False: 0.0}).astype(float)

    t0ts = pd.Timestamp(t0, tz=TZ)
    eod = t0ts + pd.Timedelta(days=1)
    fs = pd.to_datetime(df.first_seen, utc=True).dt.tz_convert(TZ)
    df['t0'] = t0.isoformat()
    df['dom'] = (eod - fs).dt.total_seconds() / 86400
    df['ppm2'] = df.price_t0 / df.area.replace(0, np.nan)
    df['log_price'] = np.log(df.price_t0.clip(lower=1))
    df['cuts_before'] = df.cuts_before.fillna(0)
    df['raises_before'] = df.raises_before.fillna(0)
    first_price = df.first_price.fillna(df.price_t0)
    df['cum_change_pct'] = (df.price_t0 / first_price - 1) * 100
    lc = pd.to_datetime(df.last_cut_at, utc=True).dt.tz_convert(TZ)
    df['days_since_cut'] = (eod - lc).dt.total_seconds() / 86400
    df['cuts_per_30d'] = df.cuts_before / (df.dom.clip(lower=1) / 30)

    k = views_lag_days(t0)
    df['views_vel'] = (df.views_t0 - df.views_tk) / k
    df['views_per_dom'] = df.views_t0 / df.dom.clip(lower=1)

    df['floor_ratio'] = df.floor / df.floors_total.replace(0, np.nan)
    df['is_first_floor'] = (df.floor == 1).astype(float).where(df.floor.notna())
    df['is_last_floor'] = (df.floor == df.floors_total).astype(float).where(df.floor.notna() & df.floors_total.notna())

    # сегмент конкуренции: ячейка ~1.1×1.05 км × комнатность (фолбэк — район)
    cell = (np.floor(df.lat / 0.01).astype('Int64').astype(str) + '_'
            + np.floor(df.lon / 0.015).astype('Int64').astype(str)).where(df.lat.notna() & df.lon.notna())
    rooms_b = df.rooms.clip(upper=5)
    seg = cell.fillna('d_' + df.district.fillna('na').astype(str)) + '|' + rooms_b.astype(str)
    g = df.groupby(seg)
    df['comp_cell_rooms'] = g.listing_id.transform('count').astype(float)
    df['comp_house'] = df.groupby('resolved_house_id').listing_id.transform('count').astype(float) \
                         .where(df.resolved_house_id.notna())
    df['seg_med_ppm2'] = g.ppm2.transform('median')
    df['ppm2_vs_seg'] = df.ppm2 / df.seg_med_ppm2 - 1
    seg_vel = g.views_vel.transform('median')
    df['rdi'] = df.views_vel / seg_vel.replace(0, np.nan)
    seg_vpd = g.views_per_dom.transform('median')
    df['rdi_dom'] = df.views_per_dom / seg_vpd.replace(0, np.nan)
    df['seg_share_new7'] = (df.dom <= 7).astype(float).groupby(seg).transform('mean')
    df['seg_med_dom'] = g.dom.transform('median')
    area_b = pd.cut(df.area, [0, 35, 45, 60, 80, 110, 1e9], labels=False)
    tg = df.groupby([rooms_b, area_b])
    df['type_share'] = tg.listing_id.transform('count') / max(len(df), 1)
    df['price_pct_in_type'] = tg.price_t0.rank(pct=True)

    sn = df.seller_norm.where(~df.seller_norm.isin(GENERIC_SELLERS) & (df.seller_norm != ''))
    df['seller_active_t0'] = df.groupby(sn).listing_id.transform('count').astype(float).where(sn.notna())
    df['seller_generic'] = sn.isna().astype(float)
    return df


def add_labels(df: pd.DataFrame, t0: date) -> pd.DataFrame:
    """Метки исхода — используют БУДУЩЕЕ (last_seen после t0), только для обучения/оценки."""
    t0ts = pd.Timestamp(t0, tz=TZ)
    ls = pd.to_datetime(df.last_seen, utc=True).dt.tz_convert(TZ)
    df = df.copy()
    df['exit14'] = (ls < t0ts + pd.Timedelta(days=14)).astype(int)
    df['exit30'] = (ls < t0ts + pd.Timedelta(days=30)).astype(int)
    df['cut30'] = df['cut30'].fillna(0).astype(int)
    return df


def to_model_matrix(df: pd.DataFrame, categories: dict[str, list] | None = None) -> pd.DataFrame:
    """Матрица признаков для LightGBM. categories — словарь уровней из обучения,
    чтобы коды категорий при прогнозе совпадали с обучением."""
    X = df.reindex(columns=FEATURES).copy()
    for c in CATEGORICAL:
        levels = (categories or {}).get(c)
        X[c] = pd.Categorical(X[c].astype('string').astype(object).where(X[c].notna()),
                              categories=levels) if levels is not None else X[c].astype('string').astype('category')
    for c in X.columns:
        if c not in CATEGORICAL:
            X[c] = pd.to_numeric(X[c], errors='coerce').astype(float)
    return X
