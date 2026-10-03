#!/usr/bin/env python3
"""Фаза C: temporally-safe датасет признаков ликвидности на дату t0.

Популяция на t0: не-дубли, first_seen <= t0, last_seen >= t0 (видели на/после t0
=> были живы на t0), цена <= 80 млн и есть фото (сегмент, который deep sweep
покрывал до 2026-09-28 — иначе last_seen для него не обновлялся и метка ложная).

Метки (могут использовать будущее — это и есть исход):
  exit30  — last_seen < t0+30d  (объявление пропало из выдачи за 30 дней)
  exit14  — last_seen < t0+14d
  cut30   — снижение цены в (t0, t0+30d]

Признаки — только данные, известные на t0:
  listing_snapshots (цена/просмотры по дням), deal_score_snapshots (скор как он был
  посчитан в тот день), price_history до t0, first_seen, статические атрибуты
  объявления/ЖК (не версионируются — ограничение, см. отчёт).
"""
import os, sys, json, re
from datetime import date, timedelta
import numpy as np, pandas as pd, psycopg2

URL = [l.split('=',1)[1].strip() for l in open(os.path.expanduser('~/krisha_bot/.env')) if l.startswith('DATABASE_URL=')][0]
VIEWS_START = date(2026, 8, 14)
GENERIC_SELLERS = {'хозяин','хозяйка','продавец','собственник','владелец','агент','риелтор','риэлтор','менеджер','отдел продаж'}

def q(conn, sql, params=None):
    return pd.read_sql_query(sql, conn, params=params)

def build(t0: date, conn) -> pd.DataFrame:
    t0s = t0.isoformat()
    k = max(1, min(7, (t0 - VIEWS_START).days))
    base = q(conn, f"""
      SELECT a.id AS listing_id, a.first_seen, a.last_seen, a.rooms, a.area, a.floor, a.floors_total,
             a.year_built, a.building_type, a.renovation, a.finish_level, a.ceiling_height, a.kitchen_area,
             a.market_type, a.is_new_build, a.seller_type, a.is_owner, a.is_urgent, a.district,
             a.lat, a.lon, a.resolved_house_id, a.developer_id,
             lower(regexp_replace(trim(coalesce(a.seller_name,'')), '\\s+', ' ', 'g')) AS seller_norm,
             coalesce(jsonb_array_length(CASE WHEN jsonb_typeof(a.photos)='array' THEN a.photos END),0) AS n_photos,
             length(coalesce(a.description,'')) AS desc_len,
             (a.floorplan_url IS NOT NULL) AS has_floorplan,
             s.price AS price_t0, s.views AS views_t0, sk.views AS views_tk
      FROM apartment_listings a
      JOIN listing_snapshots s ON s.listing_id=a.id AND s.date=%(t0)s
      LEFT JOIN listing_snapshots sk ON sk.listing_id=a.id AND sk.date=%(tk)s
      WHERE coalesce(a.is_duplicate,false)=false
        AND a.first_seen::date <= %(t0)s AND a.last_seen >= %(t0)s
        AND s.price <= 80000000 AND a.photo_url IS NOT NULL
    """, {'t0': t0s, 'tk': (t0 - timedelta(days=k)).isoformat()})
    ids = tuple(base.listing_id)
    ds = q(conn, """
      SELECT DISTINCT ON (listing_id) listing_id, score_total AS ds_total, price_score AS ds_price,
             quality_score AS ds_quality, market_score AS ds_market, risk_score AS ds_risk,
             bargain_discount_pct AS ds_discount, bargain_analogs_count AS ds_analogs,
             deal_confidence AS ds_conf, data_completeness AS ds_complete, bargain_method AS ds_method
      FROM deal_score_snapshots WHERE observed_at::date = %(t0)s AND listing_id IN %(ids)s
      ORDER BY listing_id, observed_at DESC""", {'t0': t0s, 'ids': ids})
    ph = q(conn, """
      SELECT listing_id,
        count(*) FILTER (WHERE changed_at::date <= %(t0)s AND new_price < old_price) AS cuts_before,
        count(*) FILTER (WHERE changed_at::date <= %(t0)s AND new_price > old_price) AS raises_before,
        min(changed_at) FILTER (WHERE changed_at::date <= %(t0)s AND new_price < old_price) AS first_cut_at,
        max(changed_at) FILTER (WHERE changed_at::date <= %(t0)s AND new_price < old_price) AS last_cut_at,
        (array_agg(old_price ORDER BY changed_at))[1] AS first_price_any,
        bool_or(changed_at::date > %(t0)s AND changed_at::date <= %(t30)s AND new_price < old_price) AS cut30
      FROM price_history WHERE listing_id IN %(ids)s GROUP BY listing_id""",
      {'t0': t0s, 't30': (t0 + timedelta(days=30)).isoformat(), 'ids': ids})
    # первая наблюдавшаяся цена — только из событий ДО t0, иначе цена на t0
    ph_first = q(conn, """
      SELECT DISTINCT ON (listing_id) listing_id, old_price AS first_price
      FROM price_history WHERE listing_id IN %(ids)s AND changed_at::date <= %(t0)s
      ORDER BY listing_id, changed_at""", {'t0': t0s, 'ids': ids})
    cx = q(conn, """
      SELECT c.id AS resolved_house_id, coalesce(c.housing_class, c.predicted_housing_class) AS cx_class,
             c.is_newbuild AS cx_newbuild, c.year_built AS cx_year, c.has_parking AS cx_parking,
             c.has_closed_territory AS cx_closed, c.parent_complex_id IS NOT NULL AS cx_under_umbrella,
             d.score_total AS dev_score, d.has_court_cases AS dev_court, d.avg_delay_months AS dev_delay
      FROM complexes c LEFT JOIN developers d ON d.id = c.developer_id""")
    loc = q(conn, """
      SELECT DISTINCT ON (complex_id) complex_id AS resolved_house_id, score AS loc_score,
             transport_score AS loc_transport, infra_score AS loc_infra, noise_score AS loc_noise,
             green_score AS loc_green, risk_score AS loc_risk
      FROM complex_location_scores ORDER BY complex_id, computed_at""")

    df = base.merge(ds, on='listing_id', how='left').merge(ph, on='listing_id', how='left') \
             .merge(ph_first, on='listing_id', how='left') \
             .merge(cx, on='resolved_house_id', how='left').merge(loc, on='resolved_house_id', how='left')
    t0ts = pd.Timestamp(t0, tz='Asia/Almaty')
    fs = pd.to_datetime(df.first_seen, utc=True).dt.tz_convert('Asia/Almaty')
    ls = pd.to_datetime(df.last_seen, utc=True).dt.tz_convert('Asia/Almaty')
    df['t0'] = t0s
    # --- метки
    df['exit30'] = (ls < t0ts + pd.Timedelta(days=30)).astype(int)
    df['exit14'] = (ls < t0ts + pd.Timedelta(days=14)).astype(int)
    df['cut30'] = df.cut30.fillna(False).astype(int)
    # --- DOM / цена
    df['dom'] = (t0ts + pd.Timedelta(days=1) - fs).dt.total_seconds() / 86400
    df['ppm2'] = df.price_t0 / df.area.replace(0, np.nan)
    df['log_price'] = np.log(df.price_t0.clip(lower=1))
    df['cuts_before'] = df.cuts_before.fillna(0); df['raises_before'] = df.raises_before.fillna(0)
    fp = df.first_price.fillna(df.price_t0)
    df['cum_change_pct'] = (df.price_t0 / fp - 1) * 100
    lc = pd.to_datetime(df.last_cut_at, utc=True).dt.tz_convert('Asia/Almaty')
    df['days_since_cut'] = ((t0ts + pd.Timedelta(days=1) - lc).dt.total_seconds() / 86400)
    df['cuts_per_30d'] = df.cuts_before / (df.dom.clip(lower=1) / 30)
    # --- просмотры
    df['views_vel'] = (df.views_t0 - df.views_tk) / k
    df.loc[df.views_tk.isna() | df.views_t0.isna(), 'views_vel'] = np.nan
    df['views_per_dom'] = df.views_t0 / df.dom.clip(lower=1)
    # --- этаж
    df['floor_ratio'] = df.floor / df.floors_total.replace(0, np.nan)
    df['is_first_floor'] = (df.floor == 1).astype(float)
    df['is_last_floor'] = ((df.floor == df.floors_total) & df.floor.notna()).astype(float)
    # --- сегменты конкуренции (только популяция активных на t0)
    df['cell'] = (np.floor(df.lat / 0.01).astype('Int64').astype(str) + '_' + np.floor(df.lon / 0.015).astype('Int64').astype(str)).where(df.lat.notna())
    df['rooms_b'] = df.rooms.clip(upper=5)
    df['area_b'] = pd.cut(df.area, [0, 35, 45, 60, 80, 110, 1e9], labels=False)
    seg_cell = df.cell.fillna('d_' + df.district.fillna('na')) + '|' + df.rooms_b.astype(str)
    df['comp_cell_rooms'] = df.groupby(seg_cell).listing_id.transform('count')
    df['comp_house'] = df.groupby('resolved_house_id').listing_id.transform('count').where(df.resolved_house_id.notna())
    df['seg_med_ppm2'] = df.groupby(seg_cell).ppm2.transform('median')
    df['ppm2_vs_seg'] = df.ppm2 / df.seg_med_ppm2 - 1
    df['seg_med_vel'] = df.groupby(seg_cell).views_vel.transform('median')
    df['rdi'] = df.views_vel / df.seg_med_vel.replace(0, np.nan)
    df['seg_med_views_dom'] = df.groupby(seg_cell).views_per_dom.transform('median')
    df['rdi_dom'] = df.views_per_dom / df.seg_med_views_dom.replace(0, np.nan)
    df['new7_cell_rooms'] = df.assign(n=(df.dom <= 7).astype(int)).groupby(seg_cell).n.transform('sum')
    df['seg_share_new7'] = df.new7_cell_rooms / df.comp_cell_rooms
    df['seg_med_dom'] = df.groupby(seg_cell).dom.transform('median')
    # buyer pool: доля типа (комнаты × площадь) в городском предложении
    df['type_share'] = df.groupby(['rooms_b', 'area_b']).listing_id.transform('count') / len(df)
    df['price_pct_in_type'] = df.groupby(['rooms_b', 'area_b']).price_t0.rank(pct=True)
    # --- продавец на t0
    sn = df.seller_norm.where(~df.seller_norm.isin(GENERIC_SELLERS) & (df.seller_norm != ''))
    df['seller_active_t0'] = df.groupby(sn).listing_id.transform('count').where(sn.notna())
    df['seller_generic'] = sn.isna().astype(int)
    df['is_owner'] = df.is_owner.astype(float)
    df['is_urgent'] = df.is_urgent.astype(float)
    return df

if __name__ == '__main__':
    out = sys.argv[1]
    t0s = [date.fromisoformat(x) for x in sys.argv[2:]]
    conn = psycopg2.connect(URL)
    parts = []
    for t0 in t0s:
        d = build(t0, conn); parts.append(d)
        print(t0, len(d), 'exit30=%.3f exit14=%.3f cut30=%.3f ds_cov=%.3f views_cov=%.3f' % (
            d.exit30.mean(), d.exit14.mean(), d.cut30.mean(), d.ds_price.notna().mean(), d.views_vel.notna().mean()), flush=True)
    pd.concat(parts).to_pickle(out)
