"""Страница «Качество данных» (задача 2026-10-03, план месяца п.4).

Один экран, на который смотрят перед тем, как верить цифрам аналитики:
  * каталог — сколько объявлений реально в выдаче, здоров ли обход продажи и аренды;
  * покрытие — у какой доли живых объявлений есть координаты, ЖК по id, класс,
    просмотры, Deal Score и прогноз ликвидности на сегодня;
  * свежесть — дата последнего снимка каждой расчётной таблицы;
  * сбои — systemd-юниты krisha-* / hatuli-*, у которых последний запуск неуспешен.

Каждая метрика получает статус ok / warn / bad по явным порогам (THRESHOLDS),
чтобы страница отвечала на вопрос «можно ли сейчас верить аналитике», а не
просто показывала числа. «Живое» объявление = не дубль и is_active (после
stale_archive это «видели в выдаче за 7 дней»).
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone

# (warn_below, bad_below) для долей покрытия, %
COVERAGE_THRESHOLDS = {
    'coords': (95, 85),
    'complex_id': (85, 70),
    'housing_class': (70, 50),
    'views': (60, 30),
    'deal_score_today': (90, 70),
    'liquidity_today': (80, 50),
}
# допустимый возраст снимка, дни: (warn_after, bad_after)
FRESHNESS_THRESHOLDS = {'daily': (1, 3), 'weekly': (8, 15), 'monthly': (35, 45)}


def grade_coverage(key: str, pct: float | None) -> str:
    if pct is None:
        return 'bad'
    warn, bad = COVERAGE_THRESHOLDS[key]
    return 'ok' if pct >= warn else ('warn' if pct >= bad else 'bad')


def grade_age(kind: str, last: date | None, today: date) -> str:
    if last is None:
        return 'bad'
    warn, bad = FRESHNESS_THRESHOLDS[kind]
    age = (today - last).days
    return 'ok' if age <= warn else ('warn' if age <= bad else 'bad')


def grade_cycle(completed_at: datetime | None, duration_h: float | None, now: datetime,
                target_h: float) -> str:
    if completed_at is None or not duration_h:
        return 'bad'
    age_h = (now - completed_at).total_seconds() / 3600
    if age_h > 3 * max(duration_h, target_h):
        return 'bad'
    return 'ok' if duration_h <= target_h and age_h <= 2 * target_h else 'warn'


def circle_coverage_pct(settings: dict[str, str], page_size: int = 20) -> float | None:
    """Какую долю каталога Крыши покрывает текущий круг deep sweep (снимок страниц × 20)."""
    try:
        pages = int(settings.get('DEEP_SWEEP_CIRCLE_MAX_PAGE') or settings.get('DEEP_SWEEP_CIRCLE_COMPLETED_PAGES') or 0)
        total = int(settings.get('KRISHA_TOTAL_FOUND') or 0)
    except ValueError:
        return None
    return round(min(100.0, 100.0 * pages * page_size / total), 1) if pages and total else None


def worst(*statuses: str) -> str:
    order = {'ok': 0, 'warn': 1, 'bad': 2}
    return max(statuses, key=lambda x: order[x])


def _pct(n, total) -> float | None:
    return round(100.0 * n / total, 1) if total else None


def _ts(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


FRESHNESS_SOURCES = [
    # (подпись, SQL max-даты, тип)
    ('Снимок объявлений (listing_snapshots)', "SELECT max(date) FROM listing_snapshots", 'daily'),
    ('Снимок Deal Score', "SELECT max(observed_at)::date FROM deal_score_snapshots", 'daily'),
    ('Исходы (outcome_labels)', "SELECT max(computed_at)::date FROM outcome_labels", 'daily'),
    ('Профили продавцов', "SELECT max(computed_at)::date FROM seller_profiles", 'daily'),
    ('Статистика ЖК (complex_stats_history)', "SELECT max(date) FROM complex_stats_history", 'daily'),
    ('Плотность по гексам (hex_market_stats)', "SELECT max(date) FROM hex_market_stats", 'daily'),
    ('Прогноз ликвидности', "SELECT max(as_of) FROM liquidity_predictions", 'daily'),
    ('Локационные скоры ЖК', "SELECT max(computed_at)::date FROM complex_location_scores", 'monthly'),
    ('Реестр КЖК', "SELECT max(fetched_at)::date FROM kzk_registry", 'weekly'),
]


async def _units_failed() -> list[dict]:
    """Юниты, у которых ПОСЛЕДНИЙ запуск неуспешен (Result != success) или сервис в failed."""
    proc = await asyncio.create_subprocess_exec(
        'systemctl', 'list-units', '--type=service', '--all', '--no-legend', '--plain',
        'krisha-*', 'hatuli-*', 'cbm-bridge.service', 'postgresql@*',
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    units = [ln.split(None, 4)[0] for ln in out.decode(errors='replace').splitlines() if ln.strip()]
    bad = []
    for u in units:
        p = await asyncio.create_subprocess_exec(
            'systemctl', 'show', u, '-p', 'ActiveState,Result,ExecMainExitTimestamp,Description',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        o, _ = await p.communicate()
        props = dict(ln.split('=', 1) for ln in o.decode(errors='replace').splitlines() if '=' in ln)
        if props.get('ActiveState') == 'failed' or props.get('Result', 'success') != 'success':
            bad.append({'unit': u, 'state': props.get('ActiveState'), 'result': props.get('Result'),
                        'last_exit': props.get('ExecMainExitTimestamp') or '—',
                        'desc': props.get('Description', '')})
    return bad


async def build_report(now: datetime | None = None) -> dict:
    from bot.db.pg import fetch, fetchrow, fetchval
    now = now or datetime.now(timezone.utc)
    today = now.astimezone().date()
    settings = {r['key']: r['value'] for r in await fetch(
        "SELECT key, value FROM app_settings WHERE key LIKE 'DEEP_SWEEP_%' OR key LIKE 'RENTAL_CRAWL_V1_APARTMENT_%'"
        " OR key = 'KRISHA_TOTAL_FOUND'")}

    cov = dict(await fetchrow("""
        SELECT count(*) AS total,
               count(*) FILTER (WHERE a.lat IS NOT NULL) AS coords,
               count(a.complex_id) AS complex_id,
               count(*) FILTER (WHERE coalesce(c.housing_class, c.predicted_housing_class) IS NOT NULL) AS housing_class,
               count(*) FILTER (WHERE a.views_count IS NOT NULL) AS views,
               count(*) FILTER (WHERE a.market_type = 'primary') AS primary_n,
               count(*) FILTER (WHERE a.last_seen >= now() - interval '2 days') AS seen_2d
          FROM apartment_listings a LEFT JOIN complexes c ON c.id = a.complex_id
         WHERE a.is_active IS NOT FALSE AND coalesce(a.is_duplicate, false) = false"""))
    total = cov['total']
    cov['deal_score_today'] = await fetchval("""
        SELECT count(DISTINCT s.listing_id) FROM deal_score_snapshots s JOIN apartment_listings a ON a.id = s.listing_id
         WHERE s.observed_at::date = (SELECT max(observed_at)::date FROM deal_score_snapshots)
           AND a.is_active IS NOT FALSE AND coalesce(a.is_duplicate, false) = false""")
    try:
        cov['liquidity_today'] = await fetchval("""
            SELECT count(*) FROM liquidity_predictions p JOIN apartment_listings a ON a.id = p.listing_id
             WHERE p.as_of = (SELECT max(as_of) FROM liquidity_predictions)
               AND a.is_active IS NOT FALSE AND coalesce(a.is_duplicate, false) = false""")
    except Exception:
        cov['liquidity_today'] = None
    coverage = []
    labels = {'coords': 'Координаты', 'complex_id': 'ЖК/дом по id', 'housing_class': 'Класс ЖК',
              'views': 'Просмотры', 'deal_score_today': 'Deal Score в последнем снимке',
              'liquidity_today': 'Прогноз ликвидности в последнем расчёте'}
    for k, title in labels.items():
        pct = _pct(cov.get(k) or 0, total)
        coverage.append({'key': k, 'title': title, 'n': cov.get(k), 'pct': pct, 'status': grade_coverage(k, pct),
                         'target': COVERAGE_THRESHOLDS[k][0]})
    by_method = {r['complex_resolution']: r['n'] for r in await fetch("""
        SELECT complex_resolution, count(*) n FROM apartment_listings
         WHERE is_active IS NOT FALSE AND coalesce(is_duplicate, false) = false AND complex_id IS NOT NULL
         GROUP BY 1""")}

    sale_done = _ts(settings.get('DEEP_SWEEP_CIRCLE_COMPLETED_AT'))
    sale_dur_h = float(settings.get('DEEP_SWEEP_CIRCLE_DURATION_SEC') or 0) / 3600 or None
    rent_done = _ts(settings.get('RENTAL_CRAWL_V1_APARTMENT_COMPLETED_AT'))
    rent_dur_h = float(settings.get('RENTAL_CRAWL_V1_APARTMENT_DURATION_SEC') or 0) / 3600 or None
    rent_active = await fetchval("""SELECT count(*) FROM rental_listings WHERE is_active IS NOT FALSE
                                    AND prop_type = 'apartment' AND coalesce(is_duplicate, false) = false""")
    rent_seen = await fetchval("""SELECT count(*) FROM rental_listings WHERE prop_type = 'apartment'
                                  AND last_seen >= now() - interval '7 days'""")
    catalog = {
        'sale': {'live': total, 'seen_2d': cov['seen_2d'], 'primary': cov['primary_n'],
                 'completed_at': sale_done, 'duration_h': sale_dur_h,
                 'page': settings.get('DEEP_SWEEP_PAGE'), 'max_page': settings.get('DEEP_SWEEP_CIRCLE_MAX_PAGE'),
                 'krisha_total': int(settings.get('KRISHA_TOTAL_FOUND') or 0),
                 'circle_coverage_pct': circle_coverage_pct(settings),
                 'status': worst(grade_cycle(sale_done, sale_dur_h, now, target_h=48),
                                 'ok' if (circle_coverage_pct(settings) or 0) >= 95 else 'bad')},
        'rent': {'active': rent_active, 'seen_7d': rent_seen, 'completed_at': rent_done, 'duration_h': rent_dur_h,
                 'last_pages': settings.get('RENTAL_CRAWL_V1_APARTMENT_LAST_PAGES'),
                 'status': grade_cycle(rent_done, rent_dur_h, now, target_h=36)},
    }

    freshness = []
    for title, sql, kind in FRESHNESS_SOURCES:
        try:
            last = await fetchval(sql)
        except Exception:
            last = None
        freshness.append({'title': title, 'last': last, 'kind': kind, 'status': grade_age(kind, last, today)})

    try:
        failed = await _units_failed()
    except Exception:
        failed = None
    statuses = [c['status'] for c in coverage] + [f['status'] for f in freshness] + \
               [catalog['sale']['status'], catalog['rent']['status']]
    overall = 'bad' if 'bad' in statuses or failed else ('warn' if 'warn' in statuses else 'ok')
    return {'generated_at': now, 'overall': overall, 'catalog': catalog, 'coverage': coverage,
            'by_method': by_method, 'freshness': freshness, 'failed_units': failed}
