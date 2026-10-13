"""Shared rental consumer scopes and honest collection-health explanations.

Absence from an overdue scanner is not disappearance from the market. Source
coverage remains unknown without an independently enumerated catalog. The DB
currently has no per-row contract duration; apartment rows originate from
Krisha's monthly apartment category, not from all short-term/house categories.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone


def _prefix(alias: str) -> str:
    if alias and not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*',alias):
        raise ValueError('Invalid rental SQL alias')
    return alias+'.' if alias else ''


def rental_clean_sql(alias: str = '') -> str:
    prefix = _prefix(alias)
    return (f"{prefix}prop_type = 'apartment' AND {prefix}city = 'astana' "
            f"AND COALESCE({prefix}is_duplicate, FALSE) = FALSE "
            f"AND COALESCE({prefix}dup_needs_review, FALSE) = FALSE "
            f"AND NULLIF(btrim({prefix}duplicate_of), '') IS NULL")


def rental_live_sql(alias: str = '', *, max_age_hours: int = 72) -> str:
    if type(max_age_hours) is not int or not 1<=max_age_hours<=720:
        raise ValueError('Invalid rental freshness window')
    prefix = _prefix(alias)
    return (rental_clean_sql(alias)+f" AND {prefix}is_active IS TRUE "
            f"AND {prefix}archived_at IS NULL "
            f"AND {prefix}last_seen BETWEEN now() - interval '{max_age_hours} hours' AND now()")


def rental_confirmed_archive_sql(alias: str = '') -> str:
    prefix = _prefix(alias)
    return (rental_clean_sql(alias)+f" AND {prefix}is_active IS FALSE "
            f"AND {prefix}archived_at IS NOT NULL "
            f"AND {prefix}archive_reason IN ('archived_badge', 'confirmed_gone')")


def _utc(value) -> datetime | None:
    if isinstance(value,str):
        try:
            value = datetime.fromisoformat(value.replace('Z','+00:00'))
        except ValueError:
            return None
    if not isinstance(value,datetime) or value.tzinfo is None:
        return None
    return value.astimezone(timezone.utc)


def collection_health(settings: dict, last_observed, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    observed = _utc(last_observed)
    prefix = 'RENTAL_CRAWL_V1_APARTMENT'
    completed = _utc(settings.get(prefix+'_COMPLETED_AT'))
    try:
        duration = float(settings.get(prefix+'_DURATION_SEC') or 0)
    except (ValueError,TypeError,OverflowError):
        duration = 0
    if not math.isfinite(duration) or duration<=0:
        duration = 0
    def proof_count(suffix):
        try:
            value = int(settings.get(prefix+suffix) or 0)
        except (TypeError,ValueError,OverflowError):
            value = 0
        return value if value>0 else 0
    pages = proof_count('_LAST_PAGES')
    unique = proof_count('_UNIQUE_SEEN')
    total = proof_count('_TOTAL')
    verified = settings.get(prefix+'_COMPLETED_VERIFIED')=='1'
    enumeration_evidence = bool(verified and pages and unique and total and unique>=.95*total)
    recent = bool(observed and timedelta(0)<=now-observed<=timedelta(minutes=30))
    cycle_recent = bool(completed and timedelta(0)<=now-completed<=timedelta(hours=48)
                        and 0<duration<=48*3600 and enumeration_evidence)
    status = 'recent' if recent and cycle_recent else ('stale' if observed else 'unknown')
    note = ('Сбор аренды недавно обновлялся; полнота рынка ещё требует проверки.' if status=='recent' else
            'Обход аренды отстаёт или ещё не подтверждён. Отсутствие обновлений не означает, что квартиру сдали.')
    return {'collection_status':status,'market_coverage':'unknown',
            'last_observed_at':observed.isoformat() if observed else None,
            'last_completed_at':completed.isoformat() if completed else None,
            'last_cycle_hours':round(duration/3600,2) if duration else None,
            'completed_sweep_verified':verified,'completed_pages':pages or None,
            'completed_unique_ids':unique or None,'completed_catalog_count':total or None,
            'note':note,'scope':'Астана · квартиры из раздела месячной аренды Krisha',
            'archive_note':'Архив — подтверждённое снятие объявления, а не подтверждённая сдача квартиры.'}


async def rental_consumer_health() -> dict:
    from bot.db.pg import fetch,fetchval
    rows = await fetch("SELECT key,value FROM app_settings WHERE key = ANY($1::text[])",[
        'RENTAL_CRAWL_V1_APARTMENT_COMPLETED_AT','RENTAL_CRAWL_V1_APARTMENT_DURATION_SEC',
        'RENTAL_CRAWL_V1_APARTMENT_COMPLETED_VERIFIED','RENTAL_CRAWL_V1_APARTMENT_LAST_PAGES',
        'RENTAL_CRAWL_V1_APARTMENT_UNIQUE_SEEN','RENTAL_CRAWL_V1_APARTMENT_TOTAL'])
    observed = await fetchval("SELECT max(last_seen) FROM rental_listings WHERE prop_type='apartment' AND city='astana'")
    return collection_health({row['key']:row['value'] for row in rows},observed)
