"""Разметка исхода ушедших объявлений: продано / снято / перевыставлено
(задача 2026-10-03, вход для будущей модели P(продажа)).

Почему разметка, а не парсинг: архивная страница Крыши одинакова для
проданной и снятой квартиры (бейдж «В архиве», в JSON "status":"archive") —
сигнала продажи в источнике нет. Отсюда два слоя меток (sale_outcome_labels):

* heuristic_relist (авто, ежедневно, только вторичка) — та же квартира
  появилась под НОВЫМ id в окне [уход−3д, уход+30д]: те же комнаты,
  |Δплощадь| ≤ 1 м², цена ±15%, и (тот же дом/≤150 м И тот же этаж) или
  (тот же не-generic продавец И этаж совпадает/неизвестен И то же место).
  Это НЕ продажа. Первичку не размечаем: у застройщика десятки одинаковых
  квартир в одном доме — эвристика их не различает.
* manual — человек на /admin/sale-labels. Всегда главнее авто-метки.

Модель P(продажа) обучается только при ≥ MIN_LABELS_FOR_MODEL ручных меток
sold+withdrawn (иначе сравнивать с baseline честно не на чем) — до этого
страница показывает прогресс.
"""
from __future__ import annotations

import hashlib
import math
from datetime import datetime, timezone

LABELS = ('sold', 'withdrawn', 'relisted', 'unknown')
LABEL_TITLES = {'sold': 'Продано', 'withdrawn': 'Снято', 'relisted': 'Перевыставлено', 'unknown': 'Не знаю'}
MIN_LABELS_FOR_MODEL = 300
RELIST_WINDOW_BEFORE_D = 3
RELIST_WINDOW_AFTER_D = 30
PRICE_TOLERANCE = 0.15   # релист обычно ±15% к прежней цене
GENERIC_SELLERS = frozenset({'хозяин', 'хозяйка', 'продавец', 'собственник', 'владелец', 'агент',
                             'риелтор', 'риэлтор', 'менеджер', 'отдел продаж'})


def _dist_m(lat1, lon1, lat2, lon2) -> float:
    if None in (lat1, lon1, lat2, lon2):
        return math.inf
    return math.hypot((lat1 - lat2) * 111_000, (lon1 - lon2) * 111_000 * math.cos(math.radians(51.1)))


def is_same_flat(old: dict, new: dict) -> bool:
    """Та же физическая квартира (чистая функция, проверяется тестами)."""
    if old.get('id') == new.get('id') or old.get('rooms') != new.get('rooms'):
        return False
    if old.get('area') is None or new.get('area') is None or abs(float(old['area']) - float(new['area'])) > 1:
        return False
    same_house = old.get('resolved_house_id') is not None and old.get('resolved_house_id') == new.get('resolved_house_id')
    near = _dist_m(old.get('lat'), old.get('lon'), new.get('lat'), new.get('lon')) < 150
    same_place = same_house or near
    same_floor = old.get('floor') is not None and old.get('floor') == new.get('floor')
    s_old = (old.get('seller_norm') or '').strip()
    same_seller = bool(s_old) and s_old not in GENERIC_SELLERS and s_old == (new.get('seller_norm') or '').strip()
    no_coords = old.get('lat') is None or new.get('lat') is None
    floor_unknown = old.get('floor') is None or new.get('floor') is None
    po, pn = old.get('price'), new.get('price')
    price_close = po is None or pn is None or abs(float(pn) / float(po) - 1) <= PRICE_TOLERANCE
    if not price_close:
        return False
    # У агентов/застройщиков много одинаковых квартир в одном доме — без совпадения
    # этажа «тот же продавец» связывает РАЗНЫЕ квартиры, поэтому этаж обязателен,
    # если он известен у обоих.
    return (same_place and same_floor) or (same_seller and (same_floor or floor_unknown) and (same_place or no_coords))


# ---------------------------------------------------------------- авто-метки (sync, psycopg2 + pandas)
def match_relists(ex, al):
    """Векторная версия is_same_flat для пар (ушедшее, новое). ex/al — DataFrame с колонками
    id, rooms, area, floor, resolved_house_id, lat, lon, seller_norm; у ex ещё exit_at, у al — fs.
    Кандидаты собираются блокингом (тот же дом / соседние ячейки ~220 м / тот же продавец),
    без декартова произведения по всему рынку."""
    import numpy as np
    import pandas as pd
    def prep(df):
        df = df.copy()
        df['ab'] = df.area.round()
        df['cx'] = np.floor(df.lat / 0.002)
        df['cy'] = np.floor(df.lon / 0.003)
        return df
    ex, al = prep(ex), prep(al).rename(columns={"fs": "fs_n"})
    parts = []
    for d in (-1, 0, 1):
        e = ex.assign(ab=ex.ab + d)
        parts.append(e.dropna(subset=['resolved_house_id']).merge(
            al.dropna(subset=['resolved_house_id']), on=['rooms', 'ab', 'resolved_house_id'], suffixes=('', '_n'))
            .assign(resolved_house_id_n=lambda x: x.resolved_house_id))
        sn_ok = e.seller_norm.notna() & (e.seller_norm.str.strip() != '') & ~e.seller_norm.isin(GENERIC_SELLERS)
        parts.append(e[sn_ok].merge(al, on=['rooms', 'ab', 'seller_norm'], suffixes=('', '_n'))
                     .assign(seller_norm_n=lambda x: x.seller_norm))
        el = e.dropna(subset=['cx', 'cy'])
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                parts.append(el.assign(cx=el.cx + dx, cy=el.cy + dy)
                             .merge(al.dropna(subset=['cx', 'cy']), on=['rooms', 'ab', 'cx', 'cy'], suffixes=('', '_n')))
    if not parts:
        return pd.DataFrame(columns=['id', 'id_n'])
    c = pd.concat(parts, ignore_index=True)
    c = c[(c.id != c.id_n) & ((c.area - c.area_n).abs() <= 1)
          & (c.fs_n >= c.exit_at - pd.Timedelta(days=RELIST_WINDOW_BEFORE_D))
          & (c.fs_n <= c.exit_at + pd.Timedelta(days=RELIST_WINDOW_AFTER_D))]
    dist = np.hypot((c.lat - c.lat_n) * 111_000, (c.lon - c.lon_n) * 111_000 * np.cos(np.radians(51.1)))
    same_house = c.resolved_house_id.notna() & (c.resolved_house_id == c.resolved_house_id_n)
    same_place = same_house | (dist < 150)
    same_floor = c.floor.notna() & (c.floor == c.floor_n)
    sn = c.seller_norm.fillna('').str.strip()
    same_seller = (sn != '') & ~sn.isin(GENERIC_SELLERS) & (sn == c.seller_norm_n.fillna('').str.strip())
    no_coords = c.lat.isna() | c.lat_n.isna()
    floor_unknown = c.floor.isna() | c.floor_n.isna()
    price_close = c.price.isna() | c.price_n.isna() | ((c.price_n / c.price - 1).abs() <= PRICE_TOLERANCE)
    ok = price_close & ((same_place & same_floor) | (same_seller & (same_floor | floor_unknown) & (same_place | no_coords)))
    return c.loc[ok, ['id', 'id_n']].drop_duplicates('id')


def find_relists(conn, since_days: int = 60) -> list[tuple[str, str]]:
    """[(ушедший id, новый id)] для объявлений, ушедших за последние since_days."""
    import pandas as pd
    q = """SELECT id, first_seen, last_seen, archived_at, is_active, rooms, area, floor, resolved_house_id, lat, lon, price, market_type,
                  lower(regexp_replace(trim(coalesce(seller_name,'')), '\\s+', ' ', 'g')) AS seller_norm
           FROM apartment_listings WHERE rooms IS NOT NULL AND area IS NOT NULL"""
    with conn.cursor() as cur:
        cur.execute(q)
        al = pd.DataFrame(cur.fetchall(), columns=[d[0] for d in cur.description])
    for col in ('area', 'lat', 'lon', 'floor', 'resolved_house_id', 'price'):
        al[col] = pd.to_numeric(al[col], errors='coerce').astype(float)
    al['fs'] = pd.to_datetime(al.first_seen, utc=True)
    now = pd.Timestamp.now(tz='UTC')
    arch = pd.to_datetime(al.archived_at, utc=True)
    ex = al[(al.is_active == False) & arch.notna() & (arch >= now - pd.Timedelta(days=since_days))  # noqa: E712
            & (al.market_type.fillna('secondary') != 'primary')].copy()
    ex['exit_at'] = pd.to_datetime(ex.last_seen, utc=True).fillna(arch[ex.index])
    m = match_relists(ex.drop(columns=['fs']), al)
    return list(m.itertuples(index=False, name=None))


def upsert_heuristic_relists(conn, pairs: list[tuple[str, str]]) -> int:
    """Авто-метки не перетирают ручные."""
    if not pairs:
        return 0
    from psycopg2.extras import execute_values
    with conn, conn.cursor() as cur:
        execute_values(cur, """
          INSERT INTO sale_outcome_labels (listing_id, label, source, relisted_as, labeled_by)
          VALUES %s
          ON CONFLICT (listing_id) DO UPDATE SET relisted_as = EXCLUDED.relisted_as, labeled_at = now()
          WHERE sale_outcome_labels.source = 'heuristic_relist'""",
          [(a, 'relisted', 'heuristic_relist', b, 'auto') for a, b in pairs])
        return cur.rowcount


# ---------------------------------------------------------------- админка (async)
def stable_order_key(listing_id: str) -> str:
    """Псевдослучайный, но стабильный порядок очереди — без перекоса к
    «интересным» объявлениям (иначе выборка разметки смещена)."""
    return hashlib.md5(listing_id.encode()).hexdigest()


async def label_stats() -> dict:
    from bot.db.pg import fetch, fetchval
    rows = await fetch("SELECT source, label, count(*) n FROM sale_outcome_labels GROUP BY 1, 2")
    by = {(r['source'], r['label']): r['n'] for r in rows}
    manual = {l: by.get(('manual', l), 0) for l in LABELS}
    exited_60 = await fetchval("""SELECT count(*) FROM apartment_listings WHERE is_active = FALSE
                                  AND archived_at >= now() - interval '60 days'""")
    useful = manual['sold'] + manual['withdrawn']
    return {'manual': manual, 'manual_total': sum(manual.values()),
            'auto_relisted': by.get(('heuristic_relist', 'relisted'), 0),
            'exited_60d': int(exited_60 or 0), 'model_progress': useful,
            'model_min': MIN_LABELS_FOR_MODEL, 'model_ready': useful >= MIN_LABELS_FOR_MODEL}


async def label_queue(limit: int = 20, market: str | None = None) -> list[dict]:
    """Ушедшие за 60 дней, без ручной метки и без авто-релиста. Порядок — md5(id)."""
    from bot.db.pg import fetch
    rows = await fetch("""
        SELECT a.id, a.url, a.price, a.rooms, a.area, a.floor, a.floors_total, a.address, a.complex_name,
               a.district, a.seller_name, a.seller_type, a.is_owner, a.market_type, a.photo_url,
               a.first_seen, a.last_seen, a.archived_at, a.archive_reason,
               (SELECT count(*) FROM price_history p WHERE p.listing_id = a.id AND p.new_price < p.old_price) AS cuts,
               (SELECT (array_agg(old_price ORDER BY changed_at))[1] FROM price_history p WHERE p.listing_id = a.id) AS first_price,
               a.views_count
          FROM apartment_listings a
          LEFT JOIN sale_outcome_labels s ON s.listing_id = a.id
         WHERE a.is_active = FALSE AND a.archived_at >= now() - interval '60 days'
           AND coalesce(a.is_duplicate, false) = false AND s.listing_id IS NULL
           AND ($2::text IS NULL OR a.market_type = $2)
         ORDER BY md5(a.id) LIMIT $1""", limit, market)
    out = []
    for r in rows:
        d = dict(r)
        fs, ls = d.get('first_seen'), d.get('last_seen') or d.get('archived_at')
        d['dom_days'] = (ls - fs).days if fs and ls else None
        fp = d.get('first_price')
        d['price_change_pct'] = round((d['price'] / fp - 1) * 100, 1) if fp and d.get('price') else None
        for k in ('first_seen', 'last_seen', 'archived_at'):
            d[k] = d[k].strftime('%d.%m.%Y') if d.get(k) else None
        out.append(d)
    return out


class LabelError(ValueError):
    pass


async def save_label(listing_id: str, label: str, labeled_by: str | None = None, note: str | None = None) -> None:
    from bot.db.pg import execute, fetchval
    if label not in LABELS:
        raise LabelError(f'неизвестная метка {label!r}')
    if not await fetchval("SELECT 1 FROM apartment_listings WHERE id = $1", listing_id):
        raise LabelError('объявление не найдено')
    await execute("""
        INSERT INTO sale_outcome_labels (listing_id, label, source, labeled_by, note)
        VALUES ($1, $2, 'manual', $3, $4)
        ON CONFLICT (listing_id) DO UPDATE SET label = EXCLUDED.label, source = 'manual',
            labeled_by = EXCLUDED.labeled_by, note = EXCLUDED.note, labeled_at = now()""",
        listing_id, label, labeled_by, note)


async def undo_label(listing_id: str) -> None:
    from bot.db.pg import execute
    await execute("DELETE FROM sale_outcome_labels WHERE listing_id = $1 AND source = 'manual'", listing_id)
