"""Pure rent evidence tests; no network, database, legacy index or forecast."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from bot.core.comparison_rental_estimate import (
    RENTAL_COMPARISON_SQL, estimate_rental_offer, validate_rental_estimate,
)

NOW = datetime(2026, 10, 13, 5, tzinfo=timezone.utc)
OFFER = {"market_type": "secondary", "complex_id": 10, "rooms": 2, "area": 60, "price": 30_000_000}


def rentals():
    return [{"id": str(100000+i), "complex_id": 10, "rooms": 2, "area": 59+i*.2,
             "price": price, "floor": i+1, "floors_total": 12, "is_active": True,
             "is_duplicate": False, "dup_needs_review": False, "prop_type": "apartment",
             "rent_period": "monthly", "city": "astana", "last_seen": NOW-timedelta(hours=i+1)}
            for i, price in enumerate((200_000, 220_000, 240_000, 260_000, 280_000))]


def test_range_is_asking_rents_and_safe_projection_only():
    rows = rentals()
    for row in rows:
        row.update(address="private address", phone="private contact", lat=51.1,
                   description="private", yield_pct=99, est_rent=99)
    estimate = estimate_rental_offer(OFFER, rows, NOW)
    assert estimate["monthly_median"] == 240_000
    assert (estimate["monthly_low"], estimate["monthly_high"]) == (220_000, 260_000)
    assert estimate["sample_count"] == 5
    assert estimate["asking_prices"] is True and estimate["basis"] == "same_complex"
    assert estimate["oldest_observed_at"] == (NOW-timedelta(hours=5)).isoformat()
    assert estimate["newest_observed_at"] == (NOW-timedelta(hours=1)).isoformat()
    assert set(estimate) == {"monthly_median", "monthly_low", "monthly_high", "sample_count", "observed_at",
                             "oldest_observed_at", "newest_observed_at", "date_label", "basis", "asking_prices"}
    assert validate_rental_estimate(estimate, NOW) == estimate


@pytest.mark.parametrize("field,value", [
    ("is_active", False), ("is_active", None), ("is_duplicate", True), ("is_duplicate", "false"),
    ("dup_needs_review", True), ("duplicate_of", "100001"), ("archived_at", NOW),
    ("city", "almaty"), ("prop_type", "parking"), ("rent_period", "daily"), ("rent_period", None),
    ("rooms", 3), ("complex_id", 20), ("complex_id", None), ("area", 50), ("area", 70),
    ("area", float("nan")), ("price", 49_999), ("price", 2_000_001), ("price", True),
    ("last_seen", NOW-timedelta(hours=72, seconds=1)), ("last_seen", NOW+timedelta(seconds=1)),
    ("last_seen", NOW.replace(tzinfo=None)), ("title", "Сдается посуточно"), ("title", "за сутки"),
    ("title", "rent per night"), ("title", "за 3 часа"), ("title", "тәулікке"),
    ("title", "сағат бойынша"), ("floor", 13), ("floor", 0), ("id", "private-token"),
])
def test_invalid_row_removes_estimate_when_minimum_evidence_lost(field, value):
    rows = rentals()
    rows[0][field] = value
    assert estimate_rental_offer(OFFER, rows, NOW) is None


def test_room_area_and_physical_project_are_required():
    for field in ("complex_id", "rooms", "area"):
        offer = dict(OFFER)
        offer.pop(field)
        assert estimate_rental_offer(offer, rentals(), NOW) is None
    assert estimate_rental_offer({**OFFER, "market_type": "primary", "build_status": "under_construction"}, rentals(), NOW) is None
    assert estimate_rental_offer({**OFFER, "market_type": "primary", "build_status": "unknown"}, rentals(), NOW) is None
    assert estimate_rental_offer({**OFFER, "market_type": "primary", "build_status": "commissioned"}, rentals(), NOW)


def test_no_legacy_estimate_fallback():
    offer = {**OFFER, "est_rent": 500000, "yield_pct": 30, "rental_index": {"sample_count": 100}}
    assert estimate_rental_offer(offer, [], NOW) is None
    assert estimate_rental_offer(offer, rentals()[:4], NOW) is None


def test_repeated_ids_and_conservative_signatures_do_not_inflate_sample():
    rows = rentals()[:4]
    duplicate = deepcopy(rows[0])
    assert estimate_rental_offer(OFFER, rows+[duplicate], NOW) is None
    duplicate["id"] = "999999"
    assert estimate_rental_offer(OFFER, rows+[duplicate], NOW) is None
    # A stale earlier observation for an existing ID is never another sample.
    duplicate["id"] = rows[0]["id"]
    duplicate["price"] += 50000
    duplicate["last_seen"] -= timedelta(hours=1)
    estimate = estimate_rental_offer(OFFER, rentals()+[duplicate], NOW)
    assert estimate["sample_count"] == 5 and estimate["monthly_median"] == 240000
    assert estimate_rental_offer(OFFER, list(reversed(rentals()+[duplicate])), NOW) == estimate


def test_boundary_area_and_freshness_are_inclusive():
    rows = rentals()
    rows[0]["area"] = 51
    rows[1]["area"] = 69
    rows[0]["last_seen"] = NOW-timedelta(hours=72)
    rows[1]["last_seen"] = NOW
    assert estimate_rental_offer(OFFER, rows, NOW)


def test_high_dispersion_suppresses_estimate():
    rows = rentals()
    for row, price in zip(rows, (50000, 100000, 200000, 700000, 900000)):
        row["price"] = price
    assert estimate_rental_offer(OFFER, rows, NOW) is None


@pytest.mark.parametrize("field,value", [
    ("monthly_median", "240000"), ("monthly_median", True), ("monthly_median", 300000),
    ("monthly_low", 300000), ("monthly_high", 210000), ("monthly_high", 2_000_001),
    ("monthly_high", 900000), ("sample_count", 4), ("sample_count", True), ("sample_count", 100001),
    ("basis", "city"), ("asking_prices", False), ("oldest_observed_at", "not-a-date"),
    ("observed_at", NOW+timedelta(seconds=1)), ("newest_observed_at", NOW+timedelta(seconds=1)),
])
def test_validator_rechecks_nested_snapshot(field, value):
    estimate = estimate_rental_offer(OFFER, rentals(), NOW)
    estimate[field] = value
    assert validate_rental_estimate(estimate, NOW) is None


def test_validator_expires_oldest_evidence_and_drops_private_fields():
    estimate = estimate_rental_offer(OFFER, rentals(), NOW)
    assert validate_rental_estimate(estimate, NOW+timedelta(hours=68)) is None
    estimate.update(phone="private", ids=["100000"], yield_pct=100, date_label="invented-date")
    validated = validate_rental_estimate(estimate, NOW)
    assert all(key not in validated for key in ("phone", "ids", "yield_pct"))
    assert validated["date_label"] == "13.10.2026, 09:00"


def test_sql_is_read_only_unambiguous_verified_monthly_source():
    assert "count(DISTINCT cid)=1" in RENTAL_COMPARISON_SQL
    assert "a.status='verified'" in RENTAL_COMPARISON_SQL
    assert "r.is_active IS TRUE" in RENTAL_COMPARISON_SQL
    assert "r.archived_at IS NULL" in RENTAL_COMPARISON_SQL
    assert "canonical.is_umbrella" in RENTAL_COMPARISON_SQL
    assert "interval '72 hours'" in RENTAL_COMPARISON_SQL
    assert "r.prop_type='apartment'" in RENTAL_COMPARISON_SQL
    assert "rental_index" not in RENTAL_COMPARISON_SQL
