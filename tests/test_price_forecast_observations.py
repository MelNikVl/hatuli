"""Versioned capture correctness without production database fixtures."""
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from bot.core.price_forecast_observations import (
    _atomic_private, build_groups, observation, publish_capture,
)
from scripts.collect_price_forecast_observations import collect_inputs

NOW = datetime(2026, 10, 13, 6, 0, tzinfo=timezone.utc)


def quote(**updates):
    row = {
        "listing_id": "1001", "entity_kind": "listing", "source": "krisha",
        "source_observed_at": NOW-timedelta(hours=25), "source_status": "active",
        "source_price_confirmed": True, "source_provenance": "sale_search_ingestion",
        "price": 35_000_000, "area": 56.5, "rooms": 2, "floor": 7,
        "floors_total": 12, "market_type": "secondary", "complex_id": 77,
        "property_identity_status": "provisional", "is_duplicate": False,
        "dup_needs_review": False, "address": "must never be exported",
        "seller_phone": "must never be exported", "lat": 51.1, "lon": 71.42,
    }
    row.update(updates)
    return row


def nodes(*identifiers):
    return [{"listing_id": identifier} for identifier in identifiers]


@pytest.mark.parametrize("updates", [
    {"source_observed_at": NOW+timedelta(seconds=1)},
    {"source_observed_at": NOW-timedelta(hours=48, seconds=1)},
    {"source_observed_at": NOW.replace(tzinfo=None)},
    {"source_observed_at": None},
    {"source_status": "archived"},
    {"source_price_confirmed": False},
    {"source_provenance": "daily_database_copy"},
    {"is_duplicate": True}, {"dup_needs_review": True},
    {"market_type": "rent"}, {"price": float("inf")},
    {"area": float("nan")}, {"rooms": True}, {"floor": 13},
])
def test_invalid_or_unobserved_source_fails_closed(updates):
    assert observation(quote(**updates), captured_at=NOW, group_id="g") is None


def test_capture_time_does_not_replace_source_time_or_backfill_traits():
    row = observation(quote(), captured_at=NOW, group_id="g")
    assert row["source_observed_at"] == (NOW-timedelta(hours=25)).isoformat()
    assert row["attributes_known_at"] == row["captured_at"] == NOW.isoformat()
    assert row["date"] == "2026-10-13"
    assert row["legal_availability_verified"] is False
    assert not {"address", "seller_phone", "lat"} & row.keys()
    assert row["hex_id"] and row["hex_edge_m"] == 100
    assert row["geo_quality"] == "approximate_listing_point"


def test_primary_location_is_coarse_project_anchor_and_unknown_origin_abstains():
    row = observation(quote(listing_id="newbuild:4", entity_kind="unit", source="bi_group",
                            source_provenance="developer_inventory_ingestion", market_type="primary",
                            coords_source="developer"), captured_at=NOW, group_id="g")
    assert row["geo_quality"] == "source_project_point" and row["hex_id"]
    assert not {"lat", "lon"} & row.keys()
    unknown = observation(quote(listing_id="newbuild:4", entity_kind="unit", source="bi_group",
                                source_provenance="developer_inventory_ingestion", market_type="primary"),
                          captured_at=NOW, group_id="g")
    assert unknown["hex_id"] is None and unknown["geo_quality"] == "unknown"


def test_invalid_location_is_unknown_and_reactivation_time_is_not_backfilled():
    reactivated = NOW-timedelta(hours=4)
    row = observation(quote(lat=0, latest_reactivated_at=reactivated,
                            source_observed_at=NOW-timedelta(hours=2)), captured_at=NOW, group_id="g")
    assert row["hex_id"] is None
    assert row["latest_reactivated_at"] == reactivated.isoformat()
    assert observation(quote(latest_reactivated_at=NOW+timedelta(seconds=1)), captured_at=NOW, group_id="g") is None


def test_price_quote_from_before_reactivation_is_rejected_even_if_recent():
    assert observation(quote(source_observed_at=NOW-timedelta(hours=2),
                             latest_reactivated_at=NOW-timedelta(hours=1)),
                       captured_at=NOW, group_id="g") is None
    assert observation(quote(source_observed_at=NOW-timedelta(minutes=30),
                             latest_reactivated_at=NOW-timedelta(hours=1)),
                       captured_at=NOW, group_id="g") is not None


def test_repeated_database_copy_is_not_an_independent_price_observation(tmp_path):
    first = publish_capture([quote()], nodes("1001"), [], tmp_path, captured_at=NOW)
    original = next(tmp_path.rglob("*.jsonl")).read_bytes()
    later = publish_capture([quote()], nodes("1001"), [], tmp_path, captured_at=NOW+timedelta(hours=5))
    assert first["new_observations"] == 1
    assert later["new_observations"] == 0
    assert later["unchanged_source_observations"] == 1
    assert len(list(tmp_path.rglob("*.jsonl"))) == 1
    assert next(tmp_path.rglob("*.jsonl")).read_bytes() == original


def test_repeated_rows_in_one_capture_are_not_independent(tmp_path):
    report = publish_capture([quote(), quote()], nodes("1001"), [], tmp_path, captured_at=NOW)
    assert report["new_observations"] == 1
    assert report["unchanged_source_observations"] == 1


def test_new_actual_source_observation_with_unchanged_price_is_collected(tmp_path):
    publish_capture([quote()], nodes("1001"), [], tmp_path, captured_at=NOW)
    report = publish_capture([quote(source_observed_at=NOW)], nodes("1001"), [],
                             tmp_path, captured_at=NOW+timedelta(hours=1))
    assert report["new_observations"] == 1
    assert len(list(tmp_path.rglob("*.jsonl"))) == 2


def test_changed_cached_price_without_new_source_observation_is_rejected(tmp_path):
    publish_capture([quote()], nodes("1001"), [], tmp_path, captured_at=NOW)
    report = publish_capture([quote(price=36_000_000)], nodes("1001"), [],
                             tmp_path, captured_at=NOW+timedelta(hours=1))
    assert report["new_observations"] == 0
    assert report["conflicting_cached_quotes"] == report["rejected"] == 1


def test_recovered_immutable_capture_prevents_duplicates_after_index_loss(tmp_path):
    publish_capture([quote()], nodes("1001"), [], tmp_path, captured_at=NOW)
    (tmp_path/".index.json").unlink()
    report = publish_capture([quote()], nodes("1001"), [], tmp_path, captured_at=NOW+timedelta(hours=1))
    assert report["new_observations"] == 0
    assert len(list(tmp_path.rglob("*.jsonl"))) == 1


def test_precautionary_groups_join_known_duplicate_and_shared_photo(tmp_path):
    group_nodes = [{"listing_id": "1001", "photo_key": "private-hash"},
                   {"listing_id": "1002", "photo_key": "private-hash"},
                   {"listing_id": "1003"}]
    bindings, _ = build_groups(group_nodes, [{"left_id": "1002", "right_id": "1003"}])
    assert len(set(bindings.values())) == 1
    assert "private-hash" not in json.dumps(bindings)


def test_later_group_merge_records_evidence_without_fake_price_point(tmp_path):
    rows = [quote(), quote(listing_id="1002")]
    publish_capture(rows, nodes("1001", "1002"), [], tmp_path, captured_at=NOW)
    report = publish_capture(rows, nodes("1001", "1002"),
                             [{"left_id": "1001", "right_id": "1002"}],
                             tmp_path, captured_at=NOW+timedelta(hours=1))
    assert report["new_observations"] == 0
    assert report["group_links"] > 0
    header = json.loads(open(report["capture_file"], encoding="utf-8").readline())
    assert header["record_type"] == "capture_meta"
    assert header["grouping_is_physical_identity"] is False
    assert header["group_links"]


def test_capture_is_consumed_by_forecast_reader_with_capture_day_and_group_aliases(tmp_path):
    from bot.core.price_forecast import load_observation_history

    rows = [quote(), quote(listing_id="1002")]
    publish_capture(rows, nodes("1001", "1002"), [], tmp_path, captured_at=NOW)
    publish_capture(rows, nodes("1001", "1002"), [{"left_id": "1001", "right_id": "1002"}],
                    tmp_path, captured_at=NOW+timedelta(hours=1))
    records = load_observation_history(tmp_path, as_of=NOW+timedelta(hours=2))
    assert len(records) == 2 and len({r["group_id"] for r in records}) == 1
    assert all(len(r["points"]) == 1 and r["points"][0]["date"] == "2026-10-13" for r in records)
    assert all(r["attributes_known_at"] == NOW.isoformat() for r in records)


def test_atomic_capture_cannot_overwrite_published_history(tmp_path):
    path = tmp_path/"capture.jsonl"
    _atomic_private(path, "first\n", immutable=True)
    with pytest.raises(FileExistsError):
        _atomic_private(path, "second\n", immutable=True)
    assert path.read_text() == "first\n"


class FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class FakeConnection:
    def __init__(self, read_only="on", fail=False):
        self.read_only, self.fail, self.closed = read_only, fail, False
        self.queries = []

    def transaction(self, **options):
        self.options = options
        return FakeTransaction()

    async def fetchval(self, sql):
        return self.read_only if sql.startswith("SHOW") else NOW

    async def fetch(self, sql):
        self.queries.append(sql)
        if self.fail:
            raise RuntimeError("read failed")
        return []

    async def close(self):
        self.closed = True


def connect_fake(monkeypatch, connection):
    captured = {}

    async def connect(_dsn, **options):
        captured.update(options)
        return connection

    monkeypatch.setitem(sys.modules, "asyncpg", SimpleNamespace(connect=connect))
    return captured


def test_collection_connection_is_read_only_and_features_known_after_reads(monkeypatch):
    connection = FakeConnection()
    options = connect_fake(monkeypatch, connection)
    rows, group_nodes, edges, copied = asyncio.run(collect_inputs("postgresql://unused@127.0.0.1:1/isolated"))
    assert options["server_settings"]["default_transaction_read_only"] == "on"
    assert connection.options == {"readonly": True, "isolation": "repeatable_read"}
    assert len(connection.queries) == 3 and copied == NOW
    assert not rows and not group_nodes and not edges and connection.closed


@pytest.mark.parametrize("read_only,fail,match", [("off", False, "read-only"), ("on", True, "read failed")])
def test_collection_fails_closed_and_releases_connection(monkeypatch, read_only, fail, match):
    connection = FakeConnection(read_only, fail)
    connect_fake(monkeypatch, connection)
    with pytest.raises(RuntimeError, match=match):
        asyncio.run(collect_inputs("postgresql://unused@127.0.0.1:1/isolated"))
    assert connection.closed
    if read_only == "off":
        assert not connection.queries
