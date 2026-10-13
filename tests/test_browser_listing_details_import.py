"""Importer validation/update safeguards without any database fixture."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from scripts.import_browser_listing_details import plan_row, source_projection, update_fields, validate_record

NOW = datetime(2026, 10, 13, 6, 10, tzinfo=timezone.utc)
PHOTO = "https://astps-photos-kr.kcdn.kz/webp/aa/bb/photo-750x470.webp"


def sample():
    before = {"price": 25000000, "rooms": 1, "area": 38.400001525878906, "photo_count": 0,
              "floor": None, "floors_total": None, "year_built": None, "last_seen": (NOW - timedelta(hours=5)).isoformat()}
    record = {"listing_id": "123456789", "source_listing_id": "123456789", "url": "https://krisha.kz/a/show/123456789",
              "checked_at": NOW.isoformat(), "source_active": True, "source_price": before["price"],
              "source_rooms": 1, "source_area": 38.4, "photos": [PHOTO], "floor": 5, "floors_total": 9, "year_built": 2020, "before": before}
    current = {"id": record["listing_id"], "url": record["url"], "price": before["price"], "rooms": 1,
               "area": before["area"], "last_seen": before["last_seen"], "photos": None,
               "floor": None, "floors_total": None, "year_built": None, "is_active": True,
               "is_duplicate": False, "dup_needs_review": False, "archived_at": None}
    return record, current


def test_only_missing_approved_fields_are_planned_and_float_display_is_tolerated():
    record, current = sample()
    result = plan_row(record, current, NOW)
    assert result["status"] == "planned"
    assert result["after"] == {"photos": [PHOTO], "floor": 5, "floors_total": 9, "year_built": 2020}
    assert result["before"] == {"photos": None, "floor": None, "floors_total": None, "year_built": None}
    assert set(result["after"]).isdisjoint({"price", "rooms", "area", "last_seen", "details_fetched", "complex_id", "seller_name"})


def test_existing_fields_are_preserved_even_when_source_differs():
    record, current = sample()
    current.update({"photos": ["existing"], "floor": 3, "floors_total": 8, "year_built": 2019})
    record["before"].update({"photo_count": 1, "floor": 3, "floors_total": 8, "year_built": 2019})
    assert plan_row(record, current, NOW)["status"] == "no_missing_fields"


def test_missing_total_does_not_mix_an_existing_floor_with_a_different_source_floor():
    record, current = sample()
    current["floor"] = record["before"]["floor"] = 3
    assert record["floor"] == 5
    assert plan_row(record, current, NOW)["status"] == "floor_conflict"


@pytest.mark.parametrize("changed", [{"price": 24900000}, {"area": 39}, {"rooms": 2},
                                    {"last_seen": (NOW - timedelta(hours=4)).isoformat()}, {"floor": 3}, {"photos": ["new"]}])
def test_collector_updates_block_stale_manifest(changed):
    record, current = sample()
    current.update(changed)
    assert plan_row(record, current, NOW)["status"] == "optimistic_seed_changed"


@pytest.mark.parametrize("changed", [{"is_active": False}, {"is_duplicate": True}, {"dup_needs_review": True},
                                    {"archived_at": NOW.isoformat()}, {"url": "https://krisha.kz/a/show/987654321"}])
def test_inactive_or_changed_source_identity_is_skipped(changed):
    record, current = sample()
    current.update(changed)
    assert plan_row(record, current, NOW)["status"] == "current_state_changed"


@pytest.mark.parametrize("changed", [{"source_active": False}, {"source_listing_id": "987654321"},
    {"url": "https://krisha.kz.evil/a/show/123456789"}, {"checked_at": (NOW-timedelta(hours=3)).isoformat()},
    {"photos": ["http://astps-photos-kr.kcdn.kz/photo.jpg"]}, {"photos": ["https://evil/photo.jpg"]},
    {"floor": 12, "floors_total": 9}, {"year_built": 190}, {"source_area": float("nan")}])
def test_invalid_browser_proof_rejected(changed):
    record, current = sample()
    record.update(changed)
    assert plan_row(record, current, NOW)["status"] == "invalid_manifest_record"


def test_price_conflict_is_reported_without_partial_fills():
    record, current = sample()
    record["source_price"] = 24500000
    result = plan_row(record, current, NOW)
    assert result["status"] == "source_price_mismatch"
    assert "after" not in result


def test_integral_json_float_seeds_are_normalized_for_asyncpg_integer_columns():
    record, current = sample()
    record["before"]["price"] = float(record["before"]["price"])
    record["before"]["rooms"] = 1.0
    result = plan_row(record, current, NOW)
    assert result["status"] == "planned"
    assert type(result["seed"]["price"]) is int
    assert type(result["seed"]["rooms"]) is int


def test_fractional_room_or_price_seeds_rejected():
    record, current = sample()
    record["before"]["rooms"] = record["source_rooms"] = 1.5
    assert plan_row(record, current, NOW)["status"] == "invalid_manifest_record"


def test_source_observation_remains_separate_from_cached_source_timestamp_and_price():
    record, current = sample()
    record["source_price"] = 24500000
    value = source_projection([record], NOW)
    assert value["records"][0]["source_price"] == 24500000
    assert value["records"][0]["checked_at"] == NOW.isoformat()
    assert "last_seen" not in value["records"][0]
    assert "before" not in value["records"][0]
    assert "photos" not in value["records"][0]


@pytest.mark.asyncio
async def test_update_sql_only_allowed_fields_and_optimistic_seed():
    record, current = sample()
    entry = plan_row(record, current, NOW)
    class Connection:
        async def fetchval(self, query, *args):
            self.query, self.args = query, args
            return entry["id"]
    connection = Connection()
    await update_fields(connection, entry)
    assignment = connection.query.split(" SET ")[1].split(" WHERE ")[0]
    assert all(key not in assignment for key in ("price=", "last_seen=", "details_fetched=", "complex_id="))
    assert "last_seen IS NOT DISTINCT FROM" in connection.query
    assert "price IS NOT DISTINCT FROM" in connection.query
    assert connection.args[-1] == NOW - timedelta(hours=5)
    await update_fields(connection, entry, rollback=True)
    assert connection.args[:4] == (None, None, None, None)


@pytest.mark.asyncio
async def test_unapproved_rollback_fields_are_never_updated():
    record, current = sample()
    entry = plan_row(record, current, NOW)
    entry["before"]["price"] = 1
    class Connection:
        async def fetchval(self, *_args):
            raise AssertionError("SQL must not execute")
    with pytest.raises(ValueError, match="unapproved"):
        await update_fields(Connection(), entry, rollback=True)
