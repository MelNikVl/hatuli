"""Метка релиста: после ухода (last_seen) в течение [-3д, +30д] появилось ДРУГОЕ объявление
той же квартиры: те же комнаты, |Δплощадь|<=1 м², и (тот же дом/<150 м И тот же этаж) ИЛИ
(тот же не-generic продавец И то же место). Это не ликвидность, а перевыставление."""
import numpy as np, pandas as pd, psycopg2, os
from build_dataset import URL, GENERIC_SELLERS
def relist_flags(df):
    conn = psycopg2.connect(URL)
    al = pd.read_sql_query("""SELECT id, first_seen, rooms, area, floor, resolved_house_id, lat, lon,
        lower(regexp_replace(trim(coalesce(seller_name,'')), '\\s+', ' ', 'g')) seller_norm
        FROM apartment_listings WHERE rooms IS NOT NULL AND area IS NOT NULL""", conn)
    al['fs'] = pd.to_datetime(al.first_seen, utc=True); al['ab'] = al.area.round()
    ex = df[['listing_id','last_seen','rooms','area','floor','resolved_house_id','lat','lon','seller_norm']].drop_duplicates('listing_id').copy()
    ex['ls'] = pd.to_datetime(ex.last_seen, utc=True)
    out = {}
    cands = []
    for d in (-1, 0, 1):
        e = ex.assign(ab=(ex.area.round() + d))
        cands.append(e.merge(al, on=['rooms','ab'], suffixes=('','_n')))
    c = pd.concat(cands)
    c = c[(c.id != c.listing_id) & ((c.area - c.area_n).abs() <= 1)
          & (c.fs >= c.ls - pd.Timedelta(days=3)) & (c.fs <= c.ls + pd.Timedelta(days=30))]
    dist = np.hypot((c.lat - c.lat_n) * 111000, (c.lon - c.lon_n) * 111000 * np.cos(np.radians(51.1)))
    same_seller = (c.seller_norm == c.seller_norm_n) & ~c.seller_norm.isin(GENERIC_SELLERS) & (c.seller_norm != '')
    same_place = (c.resolved_house_id.notna() & (c.resolved_house_id == c.resolved_house_id_n)) | (dist < 150)
    same_floor = c.floor.notna() & (c.floor == c.floor_n)
    # тот же этаж в том же доме — почти наверняка та же квартира; без этажа — только тот же продавец
    same = (same_place & same_floor) | (same_seller & (same_place | c.lat.isna() | c.lat_n.isna()))
    rl = set(c[same].listing_id)
    return df.listing_id.isin(rl).astype(int)
