"""Архивация объявлений, пропавших из выдачи Крыши (задача 2026-10-03).

Проблема (docs/phase_c_liquidity_report.md §1): is_active=TRUE стояло у ~107K
строк при ~19K живых. archive_check.py подтверждает уход точечным HTTP-запросом
(~40 объявлений за цикл) — на десятки тысяч ушедших бюджета нет, а без
архивации «мёртвые» объявления попадают в аналоги (get_comparables), на карту,
в счётчики и в outcome_labels.

Решение — дешёвый сигнал, который уже есть: deep sweep (service_apartments.py)
проходит ВЕСЬ каталог продажи по Астане за ~1.5-2 суток и обновляет last_seen
у каждого объявления, которое видит. Если объявление не появлялось в выдаче
STALE_DAYS дней, то за это время прошло ≥3 полных круга без него — это уход,
а не шум ре-ранжирования (для единичного пропуска остаётся HTTP-подтверждение
cold-пула в archive_check.py).

Защита от ложной массовой архивации — архивируем ТОЛЬКО при здоровом sweep:
  * последний полный круг завершён не позже MAX_CIRCLE_AGE назад;
  * его длительность ≤ STALE_DAYS/2 (в окно STALE_DAYS влезает ≥2 круга).
Если sweep сломан/стоит — ничего не трогаем (иначе остановка парсера на неделю
заархивировала бы весь рынок).

archived_at = last_seen: лучшая оценка даты ухода. Реактивация — штатная
(archive_check.verify_reactivation_candidates: last_seen > archived_at →
HTTP-проверка → is_active=TRUE + запись в listing_archive_history).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

STALE_DAYS = 7
MAX_CIRCLE_AGE = timedelta(hours=72)
ARCHIVE_REASON = 'sweep_stale'


@dataclass
class SweepHealth:
    healthy: bool
    reason: str
    completed_at: datetime | None = None
    duration_h: float | None = None


def _parse_ts(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def sweep_health(settings: dict[str, str], now: datetime) -> SweepHealth:
    """Чистая функция над app_settings (DEEP_SWEEP_CIRCLE_*)."""
    completed = _parse_ts(settings.get('DEEP_SWEEP_CIRCLE_COMPLETED_AT'))
    try:
        duration_s = float(settings.get('DEEP_SWEEP_CIRCLE_DURATION_SEC') or 0)
    except ValueError:
        duration_s = 0.0
    if completed is None:
        return SweepHealth(False, 'нет ни одного завершённого круга deep sweep')
    if duration_s <= 0:
        return SweepHealth(False, 'неизвестна длительность круга', completed)
    age = now - completed
    dur_h = duration_s / 3600
    if age > MAX_CIRCLE_AGE:
        return SweepHealth(False, f'последний круг завершён {age.total_seconds() / 3600:.0f} ч назад '
                                  f'(> {MAX_CIRCLE_AGE.total_seconds() / 3600:.0f} ч)', completed, dur_h)
    if duration_s > STALE_DAYS * 86400 / 2:
        return SweepHealth(False, f'круг длится {dur_h:.0f} ч — в окно {STALE_DAYS} дн. не влезает 2 круга',
                           completed, dur_h)
    return SweepHealth(True, 'ok', completed, dur_h)


def stale_cutoff(now: datetime) -> datetime:
    return now - timedelta(days=STALE_DAYS)


async def _settings() -> dict[str, str]:
    from bot.db.pg import fetch
    rows = await fetch("SELECT key, value FROM app_settings WHERE key LIKE 'DEEP_SWEEP_CIRCLE_%'")
    return {r['key']: r['value'] for r in rows}


async def archive_stale(*, dry_run: bool = False, now: datetime | None = None) -> dict:
    from bot.db.pg import execute, fetchval
    now = now or datetime.now(timezone.utc)
    health = sweep_health(await _settings(), now)
    cutoff = stale_cutoff(now)
    candidates = await fetchval(
        "SELECT count(*) FROM apartment_listings WHERE is_active IS NOT FALSE AND last_seen < $1", cutoff)
    result = {'healthy': health.healthy, 'health_reason': health.reason, 'cutoff': cutoff.isoformat(),
              'candidates': int(candidates or 0), 'archived': 0, 'dry_run': dry_run}
    if not health.healthy:
        logger.warning('stale archive пропущен: %s (кандидатов %s)', health.reason, candidates)
        return result
    if dry_run:
        return result
    status = await execute("""
        UPDATE apartment_listings
           SET is_active = FALSE, archived_at = last_seen, archive_reason = $2
         WHERE is_active IS NOT FALSE AND last_seen < $1""", cutoff, ARCHIVE_REASON)
    result['archived'] = int(status.split()[-1]) if status and status.startswith('UPDATE') else 0
    logger.info('stale archive: заархивировано %s (cutoff %s)', result['archived'], cutoff)
    return result


# ---------------------------------------------------------------- аренда (задача 2026-10-03)
# Тот же принцип для rental_listings: круг краулера аренды пишет
# RENTAL_CRAWL_V1_APARTMENT_{STARTED_AT,COMPLETED_AT,DURATION_SEC} (bot/core/
# rental_parser.collect_rental_page). Окно «не видели» — не меньше 2.5 кругов:
# круг аренды заметно длиннее, чем у продажи.
RENTAL_PREFIX = 'RENTAL_CRAWL_V1_APARTMENT'


def rental_stale_days(duration_s: float) -> int:
    return max(STALE_DAYS, int(-(-2.5 * duration_s // 86400)))


def rental_health(settings: dict[str, str], now: datetime) -> tuple[SweepHealth, int]:
    completed = _parse_ts(settings.get(f'{RENTAL_PREFIX}_COMPLETED_AT'))
    try:
        duration_s = float(settings.get(f'{RENTAL_PREFIX}_DURATION_SEC') or 0)
    except ValueError:
        duration_s = 0.0
    days = rental_stale_days(duration_s)
    if completed is None or duration_s <= 0:
        return SweepHealth(False, 'нет завершённого круга аренды с замером длительности'), days
    age = now - completed
    if age > timedelta(seconds=max(2 * duration_s, MAX_CIRCLE_AGE.total_seconds())):
        return SweepHealth(False, f'последний круг аренды завершён {age.total_seconds() / 3600:.0f} ч назад',
                           completed, duration_s / 3600), days
    return SweepHealth(True, 'ok', completed, duration_s / 3600), days


async def archive_stale_rentals(*, dry_run: bool = False, now: datetime | None = None) -> dict:
    from bot.db.pg import execute, fetch, fetchval
    now = now or datetime.now(timezone.utc)
    rows = await fetch("SELECT key, value FROM app_settings WHERE key LIKE $1", RENTAL_PREFIX + '%')
    health, days = rental_health({r['key']: r['value'] for r in rows}, now)
    cutoff = now - timedelta(days=days)
    candidates = await fetchval("""SELECT count(*) FROM rental_listings WHERE is_active IS NOT FALSE
                                   AND prop_type = 'apartment' AND last_seen < $1""", cutoff)
    result = {'healthy': health.healthy, 'health_reason': health.reason, 'stale_days': days,
              'cutoff': cutoff.isoformat(), 'candidates': int(candidates or 0), 'archived': 0, 'dry_run': dry_run}
    if not health.healthy or dry_run:
        return result
    status = await execute("""
        UPDATE rental_listings SET is_active = FALSE, archived_at = last_seen, archive_reason = $2
         WHERE is_active IS NOT FALSE AND prop_type = 'apartment' AND last_seen < $1""", cutoff, ARCHIVE_REASON)
    result['archived'] = int(status.split()[-1]) if status and status.startswith('UPDATE') else 0
    logger.info('stale archive аренды: %s (cutoff %s)', result['archived'], cutoff)
    return result
