"""Importer validation/update safeguards without any database fixture."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from scripts.import_browser_listing_details import (
    MAX_SOURCE_CHECKS_BYTES, merge_source_checks, plan_row, publish_source_checks,
    source_projection, update_fields, validate_record,
)

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


def checked_record(lid="123456789", at=NOW, price=24_500_000):
    return {"listing_id": lid, "source_listing_id": lid,
            "url": "https://krisha.kz/a/show/" + lid, "checked_at": at.isoformat(),
            "source_active": True, "source_price": price, "source_rooms": 1, "source_area": 38.4}


def cache(records):
    return {"schema_version": 1, "generated_at": NOW.isoformat(), "records": records}


def test_merge_preserves_other_listing_old_evidence_and_keeps_new_observation():
    old = checked_record(at=NOW - timedelta(days=2))
    new = checked_record("987654321", price=35_000_000)
    result = merge_source_checks(cache([old]), cache([new]), NOW)
    assert {r["listing_id"] for r in result["records"]} == {"123456789", "987654321"}
    assert next(r for r in result["records"] if r["listing_id"] == old["listing_id"])["checked_at"] == old["checked_at"]


def test_merge_uses_latest_timestamp_not_file_order_to_resolve_a_previous_mismatch():
    old = checked_record(at=NOW - timedelta(hours=4))
    matching = checked_record(at=NOW - timedelta(minutes=10), price=25_000_000)
    result = merge_source_checks(cache([matching, old]), cache([old]), NOW)
    assert len(result["records"]) == 1
    assert result["records"][0]["source_price"] == 25_000_000
    assert result["records"][0]["checked_at"] == matching["checked_at"]


def test_conflicting_facts_at_same_timestamp_do_not_silently_erase_a_mismatch(tmp_path):
    import json
    path = tmp_path / "checks.json"
    path.write_text(json.dumps(cache([checked_record()])), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="same timestamp"):
        publish_source_checks(path, [checked_record(price=25_000_000)], NOW)
    assert path.read_bytes() == before


@pytest.mark.parametrize("change", [
    {"source_price": float("nan")}, {"source_rooms": 1.5},
    {"source_listing_id": "987654321"}, {"url": "https://evil.test/a/show/123456789"},
    {"checked_at": (NOW + timedelta(minutes=1)).isoformat()},
])
def test_invalid_existing_cache_fails_closed_without_replacing_it(tmp_path, change):
    import json
    path = tmp_path / "checks.json"
    existing = cache([{**checked_record(), **change}])
    path.write_text(json.dumps(existing), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises((ValueError, KeyError, TypeError)):
        publish_source_checks(path, [checked_record("987654321")], NOW)
    assert path.read_bytes() == before
    assert not path.with_name(path.name + ".lock").exists()


@pytest.mark.parametrize("raw", [b"{invalid json", b'{"schema_version":2,"records":[]}',
                                  b'{"schema_version":1,"records":null}', b"x" * (MAX_SOURCE_CHECKS_BYTES + 1)],
                         ids=["malformed", "unsupported_schema", "missing_records", "oversized"])
def test_malformed_or_oversized_existing_file_is_preserved(tmp_path, raw):
    path = tmp_path / "checks.json"
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        publish_source_checks(path, [checked_record()], NOW)
    assert path.read_bytes() == raw


def test_atomic_publication_keeps_other_ids_and_new_matching_fact_resolves_mismatch(tmp_path):
    import json
    from bot.core.buyer_journey import observed_source

    path = tmp_path / "checks.json"
    publish_source_checks(path, [checked_record(), checked_record("987654321")], NOW)
    later = NOW + timedelta(hours=3)
    matching = checked_record(at=later, price=25_000_000)
    publish_source_checks(path, [matching], later)
    records = json.loads(path.read_text(encoding="utf-8"))["records"]
    assert len(records) == 2
    assert observed_source("123456789", 25_000_000, now=later, path=path)["price_matches"]
    assert observed_source("123456789", 25_000_000, now=later + timedelta(hours=3), path=path) is None
    assert observed_source("987654321", 26_000_000, now=later + timedelta(hours=3), path=path)["is_fresh"] is False


def test_concurrent_publication_lock_does_not_replace_or_remove_another_writers_lock(tmp_path):
    path = tmp_path / "checks.json"
    path.write_text("existing", encoding="utf-8")
    lock = path.with_name(path.name + ".lock")
    lock.write_text("held by another writer", encoding="utf-8")
    with pytest.raises(ValueError, match="locked"):
        publish_source_checks(path, [checked_record()], NOW)
    assert path.read_text(encoding="utf-8") == "existing"
    assert lock.read_text(encoding="utf-8") == "held by another writer"


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
