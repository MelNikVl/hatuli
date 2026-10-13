"""Conservative rent asking-price evidence for an individual public offer.

This module never reads a database or uses the legacy rental index, recorded
yield, or AI descriptions. The offline publisher supplies raw, fresh monthly
rental listings bound to one unambiguous canonical complex. The result is an
asking-price range, not an achieved rent, a yield or a promise of occupancy.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any

MAX_RENTAL_AGE = timedelta(hours=72)
MIN_RENTAL_COMPARABLES = 5
RENTAL_AREA_BAND = .15
MAX_RELATIVE_IQR = .50
_LOCAL_TIMEZONE = timezone(timedelta(hours=5))
_NONMONTHLY = re.compile(r"посуточ|сутк|почас|за\s+(?:\d+\s*)?час|за\s+ночь|(?:в|на)\s+день|тәулік|сағат|per\s+(?:day|night|hour)", re.I)

# rental_parser's apartment source is /arenda/kvartiry/astana/, the monthly
# category. The schema has no explicit rental-period column yet. The literal
# is source provenance, rather than a period inferred from an amount. Rows
# with contrary text are discarded again by estimate_rental_offer.
RENTAL_COMPARISON_SQL = """
WITH cx AS (
    SELECT id,coalesce(canonical_id,id) AS cid,complex_name_key(name) AS n
    FROM complexes
    WHERE NOT coalesce(is_garbage,false) AND NOT coalesce(is_street,false)
      AND canonical_reason IS DISTINCT FROM 'junk_unmatched'
), names AS (
    SELECT n,cid FROM cx
    UNION ALL
    SELECT a.normalized_name,cx.cid FROM complex_aliases a JOIN cx ON cx.id=a.complex_id
    WHERE a.status='verified'
), name_keys AS (
    SELECT n,min(cid) AS cid FROM names WHERE n<>''
    GROUP BY n HAVING count(DISTINCT cid)=1
)
SELECT r.id,r.price,r.area,r.rooms,r.floor,r.floors_total,r.last_seen,
       r.city,r.prop_type,r.is_active,r.is_duplicate,r.dup_needs_review,r.duplicate_of,
       r.archived_at,r.title,nk.cid AS complex_id,'monthly'::text AS rent_period
FROM rental_listings r
JOIN name_keys nk ON nk.n=complex_name_key(r.complex_name)
JOIN complexes canonical ON canonical.id=nk.cid
WHERE r.city='astana' AND r.prop_type='apartment' AND r.is_active IS TRUE
  AND NOT coalesce(r.is_duplicate,false) AND NOT coalesce(r.dup_needs_review,false)
  AND nullif(btrim(r.duplicate_of),'') IS NULL AND r.archived_at IS NULL
  AND r.last_seen BETWEEN $1::timestamptz-interval '72 hours' AND $1
  AND r.price BETWEEN 50000 AND 2000000 AND r.area BETWEEN 20 AND 300
  AND r.rooms BETWEEN 1 AND 6
  AND NOT coalesce(canonical.is_garbage,false) AND NOT coalesce(canonical.is_street,false)
  AND NOT coalesce(canonical.is_umbrella,false)
  AND canonical.canonical_reason IS DISTINCT FROM 'junk_unmatched'
ORDER BY r.last_seen DESC,r.id
"""


def _utc(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, TypeError, OverflowError):
            return None
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    try:
        return value.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _number(value: Any, low: float, high: float) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return value if math.isfinite(value) and low <= value <= high else None


def _integer(value: Any, low: int, high: int) -> int | None:
    number = _number(value, low, high)
    return int(number) if number is not None and number.is_integer() else None


def _percentile(values: list[int], proportion: float) -> float:
    position = (len(values)-1) * proportion
    lower = math.floor(position)
    upper = math.ceil(position)
    return values[lower] + (values[upper]-values[lower]) * (position-lower)


def _projection(middle: int, low: int, high: int, count: int, observed: datetime,
                oldest: datetime, newest: datetime) -> dict:
    return {"monthly_median": middle, "monthly_low": low, "monthly_high": high,
            "sample_count": count, "observed_at": observed.isoformat(),
            "oldest_observed_at": oldest.isoformat(), "newest_observed_at": newest.isoformat(),
            "date_label": newest.astimezone(_LOCAL_TIMEZONE).strftime("%d.%m.%Y, %H:%M"),
            "basis": "same_complex", "asking_prices": True}


def estimate_rental_offer(offer: dict, rental_rows: list[dict], now: datetime | None = None) -> dict | None:
    """Estimate only from at least five independent recent monthly asking rents.

    Conservatively collapse matching price/area/floor signatures even if they
    carry different source IDs. This can undercount genuine identical units,
    but avoids presenting repeated advertisements as additional evidence.
    Units not confirmed commissioned must not be described as rentable now.
    """
    now = _utc(now or datetime.now(timezone.utc))
    if now is None or not isinstance(offer, dict) or not isinstance(rental_rows, (list, tuple)):
        return None
    if offer.get("market_type") == "primary" and offer.get("build_status") != "commissioned":
        return None
    cid = _integer(offer.get("complex_id"), 1, 2_147_483_647)
    rooms = _integer(offer.get("rooms"), 1, 6)
    area = _number(offer.get("area"), 20, 300)
    if cid is None or rooms is None or area is None:
        return None
    candidates = []
    for row in rental_rows:
        if not isinstance(row, dict):
            continue
        if (row.get("is_active") is not True or (row.get("is_duplicate") is not False and row.get("is_duplicate") is not None)
                or (row.get("dup_needs_review") is not False and row.get("dup_needs_review") is not None) or row.get("duplicate_of")
                or row.get("archived_at") is not None or row.get("prop_type") != "apartment"
                or row.get("rent_period") != "monthly" or str(row.get("city") or "").lower() not in ("astana", "астана")):
            continue
        if _integer(row.get("complex_id"), 1, 2_147_483_647) != cid or _integer(row.get("rooms"), 1, 6) != rooms:
            continue
        row_area = _number(row.get("area"), 20, 300)
        price = _integer(row.get("price"), 50_000, 2_000_000)
        observed = _utc(row.get("last_seen"))
        identifier = str(row.get("id") or "")
        if (not re.fullmatch(r"\d{5,20}", identifier) or price is None or row_area is None
                or not area*(1-RENTAL_AREA_BAND) <= row_area <= area*(1+RENTAL_AREA_BAND)
                or observed is None or not now-MAX_RENTAL_AGE <= observed <= now
                or _NONMONTHLY.search(str(row.get("title") or ""))):
            continue
        floor = _integer(row.get("floor"), 1, 60)
        floors_total = _integer(row.get("floors_total"), 1, 60)
        if row.get("floor") is not None and floor is None:
            continue
        if row.get("floors_total") is not None and floors_total is None:
            continue
        if floor is not None and floors_total is not None and floor > floors_total:
            continue
        candidates.append((observed, identifier, price, round(row_area, 1), floor))
    # Pick the newest fact for repeated source IDs before signature collapsing.
    seen_ids, seen_signatures, comparable = set(), set(), []
    for observed, identifier, price, row_area, floor in sorted(candidates, reverse=True):
        if identifier in seen_ids:
            continue
        seen_ids.add(identifier)
        signature = (price, row_area, floor)
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)
        comparable.append((price, observed))
    if len(comparable) < MIN_RENTAL_COMPARABLES:
        return None
    prices = sorted(item[0] for item in comparable)
    middle = median(prices)
    low, high = _percentile(prices, .25), _percentile(prices, .75)
    if (high-low)/middle > MAX_RELATIVE_IQR:
        return None
    observed = [item[1] for item in comparable]
    result = _projection(round(middle), round(low), round(high), len(comparable), now, min(observed), max(observed))
    return validate_rental_estimate(result, now)


def validate_rental_estimate(value: Any, now: datetime | None = None) -> dict | None:
    """Return a strict, fresh public projection or no estimate.

    Recheck the range, dispersion and every observation boundary at request
    time. Discard unknown/private keys and recompute the display date. Yield
    and comparisons with deposits are intentionally absent from this schema.
    """
    now = _utc(now or datetime.now(timezone.utc))
    if now is None or not isinstance(value, dict) or value.get("basis") != "same_complex" or value.get("asking_prices") is not True:
        return None
    numbers = []
    for field in ("monthly_median", "monthly_low", "monthly_high"):
        raw = value.get(field)
        if isinstance(raw, bool) or not isinstance(raw, int) or not 50_000 <= raw <= 2_000_000:
            return None
        numbers.append(raw)
    middle, low, high = numbers
    count = value.get("sample_count")
    if (isinstance(count, bool) or not isinstance(count, int) or not MIN_RENTAL_COMPARABLES <= count <= 100_000
            or not low <= middle <= high or (high-low)/middle > MAX_RELATIVE_IQR):
        return None
    observed, oldest, newest = (_utc(value.get(field)) for field in
                                ("observed_at", "oldest_observed_at", "newest_observed_at"))
    if (None in (observed, oldest, newest) or not now-MAX_RENTAL_AGE <= oldest <= newest <= observed <= now):
        return None
    return _projection(middle, low, high, count, observed, oldest, newest)
