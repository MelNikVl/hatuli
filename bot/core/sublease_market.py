"""Read-only, bounded stage-3 market evidence. No source crawl or writes."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
import re

import asyncpg

from bot.core.complex_membership import rental_identity_ctes_sql
from bot.core.rental_data_quality import rental_live_sql
from bot.core.sublease import SubleaseError, number
from bot.db.pg import fetch

TTL = timedelta(hours=72)
POLICIES = {("monthly", None): "3", ("daily", None): "daily_v1",
            ("daily", "full"): "daily_v1", ("monthly", "full"): "furnished_monthly_v1"}
INDEX_COLUMNS = """i.city,i.district,i.complex_id,i.complex_name,i.rooms,i.prop_type,
    i.rent_period,i.furnishing,i.median_price,i.p25_price,i.p75_price,i.sample_count,
    i.updated_at,i.source_oldest_at,i.source_newest_at,i.window_days,i.policy_version,i.active_supply,
    i.furnishing_version,i.median_dom_days,i.gone_30d,i.months_of_supply"""
INDEX_GATE = """
    i.prop_type='apartment' AND i.rooms=$4::integer AND i.city='astana'
    AND i.rent_period IN ('monthly','daily')
    AND i.window_days IN (30,90) AND (i.rent_period='daily' OR i.window_days=30)
    AND i.updated_at BETWEEN $5::timestamptz-interval '72 hours' AND $5
    AND i.source_newest_at BETWEEN $5::timestamptz-interval '72 hours' AND i.updated_at
    AND i.source_oldest_at BETWEEN $5::timestamptz-i.window_days*interval '1 day' AND i.source_newest_at
    AND (i.rent_period<>'monthly' OR i.source_oldest_at >= $5::timestamptz-interval '72 hours')
    AND (i.policy_version<>'3' OR EXISTS (SELECT 1 FROM app_settings config
        WHERE config.key='RENTAL_INDEX_POLICY_VERSION' AND config.value='3'))
    AND ((i.rent_period='monthly' AND i.furnishing IS NULL AND i.policy_version='3')
      OR (i.rent_period='monthly' AND i.furnishing='full' AND i.policy_version='furnished_monthly_v1')
      OR (i.rent_period='daily' AND i.furnishing IS NULL AND i.policy_version='daily_v1'))
    AND i.sample_count>=CASE WHEN i.complex_id IS NOT NULL THEN $6::integer ELSE $7::integer END
    AND i.active_supply>0
    AND i.p25_price>0 AND i.p25_price<=i.median_price AND i.median_price<=i.p75_price
    AND (i.complex_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM rental_roots canonical WHERE canonical.id=i.complex_id AND NOT canonical.is_umbrella)
      OR i.complex_id IS NULL AND i.complex_name IS NULL AND NULLIF(btrim(i.district),'') IS NOT NULL)
"""
SEGMENT_SQL = rental_identity_ctes_sql() + f"""
    SELECT {INDEX_COLUMNS} FROM rental_index i
    WHERE {INDEX_GATE}
      AND (i.complex_id=(SELECT map.cid FROM rental_canonical_map map WHERE map.id=$3::integer)
        OR i.complex_id IS NULL AND i.district=COALESCE((SELECT c.district FROM complexes c
            JOIN rental_canonical_map map ON map.cid=c.id AND map.id=$3::integer),$2::text))
      AND $1::text='astana'
    ORDER BY (i.complex_id IS NOT NULL) DESC,i.window_days ASC LIMIT 12
"""


def _timestamp(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return value.astimezone(timezone.utc) if isinstance(value, datetime) and value.tzinfo else None


def ready_index(row, *, now=None, minimum_complex=5, minimum_area=10, minimum_hex=5):
    """Recheck the SQL contract before projecting or calculating any row."""
    now = now or datetime.now(timezone.utc)
    if not isinstance(row, dict):
        return None
    period, furnishing = row.get("rent_period"), row.get("furnishing")
    scope = "hex" if row.get("hex_id") is not None else "complex" if row.get("complex_id") is not None else "district"
    policy = (("daily_v1" if period == "daily" else "hex_monthly_v1")
              if scope == "hex" and furnishing is None else POLICIES.get((period, furnishing)))
    minimum = max(5, minimum_hex) if scope == "hex" else minimum_complex if scope == "complex" else minimum_area
    updated, oldest, newest = (_timestamp(row.get(key)) for key in
                               ("updated_at", "source_oldest_at", "source_newest_at"))
    n, window = row.get("sample_count"), row.get("window_days")
    if (period not in ("monthly", "daily") or not policy or row.get("policy_version") != policy or row.get("city") != "astana"
            or row.get("prop_type") != "apartment" or type(n) is not int or n < minimum
            or type(window) is not int or window not in ((30, 90) if period == "daily" else (30,))
            or None in (updated, oldest, newest)
            or not now - TTL <= newest <= updated <= now
            or not now - timedelta(days=window) <= oldest <= newest
            or period == "monthly" and oldest < now - TTL
            or scope == "district" and (not row.get("district") or row.get("complex_name") is not None)
            or type(row.get("rooms")) is not int or not 0 <= row["rooms"] <= 10):
        return None
    if type(row.get("active_supply")) is not int or row["active_supply"] <= 0:
        return None
    try:
        low, mid, high = [int(number(row.get(key), key, 1, 20_000_000, integer=True))
                          for key in ("p25_price", "median_price", "p75_price")]
        if not low <= mid <= high:
            return None
    except SubleaseError:
        return None
    if scope == "complex" and (type(row.get("complex_id")) is not int or row["complex_id"] <= 0):
        return None
    if scope == "hex":
        try:
            number(row.get("hex_edge_m"), "hex_edge_m", 1, 5000)
        except SubleaseError:
            return None
        if not isinstance(row["hex_id"], str) or not re.fullmatch(r"-?[0-9]{1,6}:-?[0-9]{1,6}", row["hex_id"]):
            return None
    active = row.get("active_supply")
    result = {"median_price": mid, "p25_price": low, "p75_price": high, "rent_period": period,
            "furnishing": furnishing, "rooms": row["rooms"], "district": row.get("district"),
            "complex_id": row.get("complex_id"), "level": scope,
            "confidence": "high" if scope == "complex" else "medium", "n": n,
            "updated_at": updated.isoformat(), "source_oldest_at": oldest.isoformat(),
            "source_newest_at": newest.isoformat(), "window_days": window, "policy_version": policy,
            "active_supply": active if type(active) is int and active >= 0 else None,
            "price_kind": "asking", "outcome_known": False}
    if scope == "hex":
        result.update(hex_id=row["hex_id"], hex_edge_m=row["hex_edge_m"])
    def metric(key, low, high, integer=False):
        try:
            value = number(row.get(key), key, low, high, integer=integer)
            return int(value) if integer else float(value)
        except SubleaseError:
            return None
    result["removal_metrics"] = {"median_observed_days": metric("median_dom_days", 2, 36500),
        "confirmed_removals_30d": metric("gone_30d", 0, 1_000_000, True),
        "months_of_supply": metric("months_of_supply", 0, 1_000_000),
        "meaning": "confirmed_ad_removals_not_signed_leases"}
    return result


def thresholds(settings):
    # A setting may tighten the documented minimum, never lower it.
    def read(key, floor):
        try:
            return max(floor, int(number(settings.get(key, str(floor)), key, 1, 10_000, integer=True)))
        except SubleaseError:
            return floor
    return read("RENT_MIN_SAMPLE_COMPLEX", 5), read("RENT_MIN_SAMPLE_AREA", 10)


async def read_segment(*, district=None, complex_id=None, rooms, settings, now=None):
    now = now or datetime.now(timezone.utc)
    if not district and complex_id is None:
        return {"monthly": None, "daily": None, "furnished_monthly": None}
    minimum_complex, minimum_area = thresholds(settings)
    try:
        rows = await fetch(SEGMENT_SQL, "astana", district, complex_id, rooms, now, minimum_complex, minimum_area)
    except (asyncpg.UndefinedColumnError, asyncpg.UndefinedTableError):
        return {"monthly": None, "daily": None, "furnished_monthly": None}
    selected = {"monthly": None, "daily": None, "furnished_monthly": None}
    for raw in rows[:12]:
        row = ready_index(dict(raw), now=now, minimum_complex=minimum_complex, minimum_area=minimum_area)
        if not row:
            continue
        kind = "furnished_monthly" if row["furnishing"] == "full" else row["rent_period"]
        if kind == "furnished_monthly" and not furnished_row_ready(dict(raw), settings):
            continue
        if selected[kind] is None:
            selected[kind] = row
    return selected


def furnished_enabled(settings):
    try:
        from bot.core.rental_furnishing import furnishing_validation_ready
    except ImportError:
        return False
    return furnishing_validation_ready(settings)


def furnished_row_ready(row, settings):
    try:
        from bot.core.rental_furnishing import furnishing_index_row_ready
    except ImportError:
        return False
    return furnishing_index_row_ready(row, settings)


async def read_hex_segment(hex_id, rooms, settings):
    result = {"monthly": None, "daily": None, "furnished_monthly": None}
    try:
        from bot.core.rental_stage2_index import lookup_hex_rental_estimate
    except ImportError:
        return result
    edge = float(number(settings.get("HEX_EDGE_M", "50"), "hex_edge_m", 1, 5000))
    for period in ("monthly", "daily"):
        raw = await lookup_hex_rental_estimate(hex_id, rooms, rent_period=period, hex_edge_m=edge)
        # The Stage-2 reader owns spatial sample/provenance validation. Avoid
        # relabeling a district aggregate as a hex when it has no local sample.
        if raw and raw.get("hex_id") == hex_id and raw.get("hex_edge_m") == edge:
            # The spatial table has no prop_type; the source reader fixes
            # apartments and Astana, while this projection rechecks metadata.
            minimum = int(number(settings.get("RENT_MIN_SAMPLE_HEX", "5"), "minimum_hex", 5, 10_000, integer=True))
            result[period] = ready_index({**dict(raw), "prop_type": "apartment"}, minimum_hex=minimum)
    return result


# Raw listing fields are read only after the paid tier gate (or for a single
# free calculation by ID, without returning description, address or contacts).
CANDIDATES_SQL = rental_identity_ctes_sql() + f""", qualified_indices AS (
    SELECT {INDEX_COLUMNS} FROM rental_index i WHERE {INDEX_GATE}
), competition AS (
    SELECT (i.complex_id IS NOT NULL) AS complex_scope,
           percentile_cont(0.75) WITHIN GROUP (ORDER BY i.active_supply) AS supply_p75
    FROM qualified_indices i WHERE i.rent_period='daily' AND i.furnishing IS NULL AND i.active_supply>=0
    GROUP BY (i.complex_id IS NOT NULL) HAVING count(*)>=5
), eligible_rentals AS (
    SELECT r.*,canonical.id AS resolved_complex_id,canonical.name AS resolved_complex_name
    FROM rental_listings r
    LEFT JOIN rental_canonical_map identity ON identity.id=r.complex_id
    LEFT JOIN rental_roots canonical ON canonical.id=identity.cid AND NOT canonical.is_umbrella
    LEFT JOIN rental_source_keys source ON source.url=rtrim(r.complex_url,'/')
      AND r.complex_url ~ '^https://krisha[.]kz/complex/show/[^/?#]+(/[^/?#]+)?/?$'
      AND (source.cid=canonical.id OR source.cid=canonical.parent_id)
    WHERE {rental_live_sql('r')}
      AND r.price BETWEEN 50000 AND 2000000 AND r.rooms=$4::integer
      AND ($2::text IS NULL OR r.district=$2)
      AND ($3::integer IS NULL OR canonical.id=(SELECT map.cid FROM rental_canonical_map map WHERE map.id=$3))
      AND (r.complex_id IS NOT NULL AND canonical.id IS NOT NULL AND source.cid IS NOT NULL
        OR r.complex_id IS NULL AND r.hex_id IS NOT NULL AND r.hex_edge_m=$8::real
           AND r.lat BETWEEN 50.9 AND 51.4 AND r.lon BETWEEN 71.1 AND 71.8)
      AND ($9::text IS NULL OR r.hex_id=$9 AND r.hex_edge_m=$8::real)
      AND ($10::integer IS NULL OR r.price<=$10)
      AND ($11::text IS NULL OR r.id=$11)
      AND $1::text='astana'
)
SELECT r.id,r.price,r.rooms,r.area,r.district,r.resolved_complex_name AS complex_name,r.resolved_complex_id AS complex_id,
       r.hex_id,r.hex_edge_m,r.lat,r.lon,r.furnishing,r.furnishing_version,r.last_seen,
       r.is_active,r.is_duplicate,r.dup_needs_review,r.rent_period,r.rent_period_conflict,
       (r.photo_url IS NOT NULL OR jsonb_array_length(CASE WHEN jsonb_typeof(r.photos)='array'
             THEN r.photos ELSE '[]'::jsonb END)>0) AS has_photo,
       row_to_json(monthly)::jsonb AS monthly_index,
       row_to_json(daily)::jsonb AS daily_index,
       row_to_json(full_monthly)::jsonb AS furnished_monthly_index,
       (SELECT competition.supply_p75 FROM competition
           WHERE competition.complex_scope=(daily.complex_id IS NOT NULL)) AS competition_p75
FROM eligible_rentals r
JOIN LATERAL (
    SELECT i.* FROM qualified_indices i WHERE i.rent_period='monthly' AND i.furnishing IS NULL
      AND (i.complex_id=r.resolved_complex_id OR i.complex_id IS NULL AND i.district=r.district)
    ORDER BY (i.complex_id IS NOT NULL) DESC LIMIT 1
) monthly ON TRUE
LEFT JOIN LATERAL (
    SELECT i.* FROM qualified_indices i WHERE i.rent_period='daily' AND i.furnishing IS NULL
      AND (i.complex_id=r.resolved_complex_id OR i.complex_id IS NULL AND i.district=r.district)
    ORDER BY (i.complex_id IS NOT NULL) DESC,i.window_days ASC LIMIT 1
) daily ON TRUE
LEFT JOIN LATERAL (
    SELECT i.* FROM qualified_indices i WHERE i.rent_period='monthly' AND i.furnishing='full'
      AND (i.complex_id=r.resolved_complex_id OR i.complex_id IS NULL AND i.district=r.district)
    ORDER BY (i.complex_id IS NOT NULL) DESC LIMIT 1
) full_monthly ON TRUE
WHERE ($13::boolean IS FALSE OR r.price<=monthly.median_price*$12::numeric)
ORDER BY r.price::numeric/monthly.median_price,r.id LIMIT 501
"""


async def read_candidates(filters, settings, *, now=None, listing_id=None, require_cheap=True):
    now = now or datetime.now(timezone.utc)
    minimum_complex, minimum_area = thresholds(settings)
    edge = float(number(settings.get("HEX_EDGE_M", "50"), "hex_edge_m", 1, 5000))
    ratio = number(settings.get("SUBLEASE_CHEAP_RATIO", "0.95"), "cheap_ratio", .1, 1)
    try:
        rows = await fetch(CANDIDATES_SQL, "astana", filters.get("district"), filters.get("complex_id"),
                           filters["rooms"], now, minimum_complex, minimum_area, edge, filters.get("hex_id"),
                           filters.get("max_rent"), listing_id, ratio, require_cheap)
    except (asyncpg.UndefinedColumnError, asyncpg.UndefinedTableError):
        return [], False
    return [dict(row) for row in rows[:500]], len(rows) > 500


def public_candidate(row):
    identifier = str(row.get("id") or "")
    if not re.fullmatch(r"[0-9]{5,20}", identifier):
        return None
    result = {key: row.get(key) for key in ("price", "rooms", "area", "district", "complex_id", "furnishing")}
    result.update(id=identifier, source_url="https://krisha.kz/a/show/"+identifier,
                  complex_name=str(row.get("complex_name") or "")[:160])
    lat, lon = row.get("lat"), row.get("lon")
    def coordinate(value, low, high):
        return (isinstance(value, (float, int)) and not isinstance(value, bool)
                and math.isfinite(value) and low <= value <= high)
    result["location"] = ({"lat": lat, "lon": lon, "precision": "source"}
                          if coordinate(lat, 50.9, 51.4) and coordinate(lon, 71.1, 71.8) else None)
    return result
