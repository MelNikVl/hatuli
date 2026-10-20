"""Persist observed sale details without dating old attributes as fresh.

Catalog ingestion remains separate. A single parameterized UPDATE applies
the detail field mask and its successful-check timestamp together. Absent
keys preserve stored data; an explicit masked JSON null clears a known
unknown value. Current parsers only emit that null with source evidence.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging

log = logging.getLogger(__name__)
DETAIL_FIELDS = (
    'description', 'floor', 'floors_total', 'year_built', 'building_type',
    'renovation', 'furniture', 'kitchen_area', 'ceiling_height', 'photos',
    'lat', 'lon', 'seller_type', 'is_owner', 'area', 'rooms', 'address',
    'views_count', 'seller_name', 'is_urgent', 'photo_url',
    'complex_name', 'complex_url',
)
DETAIL_ONLY_FIELDS = (
    'description', 'year_built', 'building_type', 'renovation', 'furniture',
    'kitchen_area', 'ceiling_height', 'photos', 'lat', 'lon', 'views_count',
    'seller_name', 'is_urgent',
)


def observation_time(value, *, now=None):
    try:
        result = datetime.fromisoformat(value) if isinstance(value, str) else value
    except ValueError:
        return None
    if not isinstance(result, datetime) or result.tzinfo is None:
        return None
    result = result.astimezone(timezone.utc)
    now = now or datetime.now(timezone.utc)
    return result if result <= now + timedelta(minutes=5) else None


def catalog_only_row(row):
    """No detail-derived field can leak into the earlier catalog write."""
    result = dict(row)
    for field in DETAIL_ONLY_FIELDS:
        result.pop(field, None)
    values = row.get('_source_detail_values')
    if isinstance(values, dict):
        for field in DETAIL_FIELDS:
            if field in values:
                result.pop(field, None)
    result['details_fetched'] = False  # INSERT default; UPDATE preserves old bool
    return result


_FRESH = "($3::timestamptz IS NOT NULL AND (a.source_details_checked_at IS NULL OR a.source_details_checked_at <= $3::timestamptz))"
_IDENTITY_INPUTS = ('complex_name', 'complex_url', 'address', 'description', 'lat', 'lon')


def _changed(fields):
    # A missing field is not a new observation. JSON null is an observed clear.
    return '(' + ' OR '.join(
        f"($2::jsonb ? '{field}' AND a.{field} IS DISTINCT FROM (incoming.detail).{field})"
        for field in fields) + ')'


_HOUSE_RESET = f"({_FRESH} AND {_changed(_IDENTITY_INPUTS)})"
_COMPLEX_RESET = f"({_FRESH} AND ({_changed(('complex_name', 'complex_url'))} OR " \
    f"(a.complex_resolution IN ('house','geo') AND {_changed(_IDENTITY_INPUTS)})))"

DETAIL_UPDATE_SQL = """WITH incoming AS (
    SELECT jsonb_populate_record(NULL::apartment_listings, $2::jsonb) AS detail
), previous AS MATERIALIZED (
    SELECT * FROM apartment_listings WHERE id=$1 FOR UPDATE
), invalidation_log AS (
    INSERT INTO listing_complex_resolution_log
        (listing_id,complex_id,resolution_method,confidence_tier,resolved_at,
         complex_name_at_resolution,evidence,resolver_version,
         previous_complex_id,previous_resolution_method)
    SELECT a.id,CASE WHEN """ + _COMPLEX_RESET + """ THEN NULL ELSE a.complex_id END,
           CASE WHEN """ + _COMPLEX_RESET + """ THEN 'unbound' ELSE coalesce(a.complex_resolution,'unbound') END,
           CASE WHEN """ + _COMPLEX_RESET + """ OR a.complex_id IS NULL THEN 'C'
                WHEN a.complex_resolution='geo' THEN 'B' ELSE 'A' END,now(),
           CASE WHEN $2::jsonb ? 'complex_name' THEN (incoming.detail).complex_name ELSE a.complex_name END,
           jsonb_build_object('reason','source_identity_evidence_changed',
               'source_observed_at',$3::timestamptz,'previous_house_id',a.resolved_house_id,
               'previous_house_attribution',a.house_attribution),
           'sale_detail_invalidation_v1',a.complex_id,a.complex_resolution
    FROM previous a CROSS JOIN incoming
    WHERE (a.complex_id IS NOT NULL AND """ + _COMPLEX_RESET + """)
       OR (a.resolved_house_id IS NOT NULL AND """ + _HOUSE_RESET + """)
    RETURNING log_id
)
UPDATE apartment_listings AS a SET
""" + ',\n'.join(
    f"    {field}=CASE WHEN {_FRESH} AND $2::jsonb ? '{field}' THEN (incoming.detail).{field} ELSE a.{field} END"
    for field in DETAIL_FIELDS
) + f""",
    resolved_house_id=CASE WHEN {_HOUSE_RESET} THEN NULL ELSE a.resolved_house_id END,
    house_attribution=CASE WHEN {_HOUSE_RESET} THEN NULL ELSE a.house_attribution END,
    house_attribution_detail=CASE WHEN {_HOUSE_RESET} THEN NULL ELSE a.house_attribution_detail END,
    complex_id=CASE WHEN {_COMPLEX_RESET} THEN NULL ELSE a.complex_id END,
    complex_resolution=CASE WHEN {_COMPLEX_RESET} THEN 'unbound' ELSE a.complex_resolution END,
    geo_source=CASE WHEN {_FRESH} AND $2::jsonb ? 'lat' AND $2::jsonb ? 'lon'
        AND (incoming.detail).lat IS NOT NULL AND (incoming.detail).lon IS NOT NULL
        THEN 'krisha' ELSE a.geo_source END,
    source_details_checked_at=GREATEST(a.source_details_checked_at, $3::timestamptz),
    source_details_attempted_at=GREATEST(a.source_details_attempted_at, $4::timestamptz),
    details_fetched=CASE WHEN {_FRESH} THEN TRUE ELSE a.details_fetched END
FROM incoming, previous WHERE a.id=previous.id"""


async def save_source_details(row, *, execute=None, now=None):
    checked = observation_time(row.get('_source_details_checked_at'), now=now)
    attempted = observation_time(row.get('_source_details_attempted_at'), now=now)
    if checked is None and attempted is None:
        return None
    values = row.get('_source_detail_values')
    patch = {key: value for key, value in values.items() if key in DETAIL_FIELDS} if checked and isinstance(values, dict) else {}
    encoded = json.dumps(patch, ensure_ascii=False, allow_nan=False)
    if execute is None:
        from bot.db.pg import execute
    status = await execute(DETAIL_UPDATE_SQL, str(row['id']), encoded, checked, attempted)
    if status != 'UPDATE 1':
        raise RuntimeError('Source detail fields/timestamps were not persisted')
    return status


async def persist_actual_attempts(attempts, *, execute=None, now=None):
    """Cancellation bookkeeping only; never insert a fake market row."""
    if execute is None:
        from bot.db.pg import execute
    for identifier, raw in attempts.items():
        observed = observation_time(raw, now=now)
        if observed is None:
            continue
        try:
            await execute('UPDATE apartment_listings SET source_details_attempted_at='
                          'GREATEST(source_details_attempted_at,$2::timestamptz) WHERE id=$1',
                          str(identifier), observed)
        except Exception:
            log.exception('Could not persist a cancelled source-detail attempt')


async def analyze_with_attempt_capture(analyze, *args, prior_results=(), **kwargs):
    try:
        return await analyze(*args, **kwargs)
    except asyncio.CancelledError as exc:
        attempts = {str(row['id']): row['_source_details_attempted_at'] for row in prior_results
                    if row.get('_source_details_attempted_at')}
        attempts.update(getattr(exc, 'sale_detail_attempts', {}) or {})
        try:
            await asyncio.shield(persist_actual_attempts(attempts))
        except asyncio.CancelledError:
            # A repeated shutdown can interrupt bookkeeping; it must never
            # turn a cancelled source response into a successful check.
            log.warning('Shutdown interrupted source-detail attempt bookkeeping')
        raise
