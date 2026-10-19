"""Isolated checks for the production read-only audit utility; never opens PG."""
from datetime import datetime, timedelta, timezone

import pytest

from scripts.audit_buyer_journey import (
    aggregate, assert_readonly, forbidden, history_checks, safe_sample, validate_ids,
)


def test_sample_projection_excludes_private_values():
    row = {"id": "123456789", "url": "https://krisha.kz/a/show/123456789", "price": 25000000,
           "photos": '["https://photos.krisha.kz/x.jpg"]', "phone": "+7000000000",
           "seller_name": "Private Seller", "address": "private address", "lat": 51.123456,
           "lon": 71.123456, "description": "Contact +7000000000"}
    value = safe_sample(row)
    assert value["id"] == row["id"]
    assert value["photo_count"] == 1
    assert set(value).isdisjoint({"photos", "phone", "seller_name", "address", "lat", "lon", "description"})


@pytest.mark.parametrize("rows", [[{"id": "12345"}, {"id": "12345"}], [{"id": "evil"}], [], [{"id": "12345"}] * 101])
def test_invalid_selection_rejected(rows):
    with pytest.raises(ValueError):
        validate_ids({"rows": rows})


def test_numeric_selection_accepted():
    assert validate_ids({"rows": [{"id": 123456789}]}) == ["123456789"]


def test_history_rejects_chain_gap_and_current_mismatch():
    now = datetime.now(timezone.utc)
    rows = [{"old_price": 30, "new_price": 29, "changed_at": now},
            {"old_price": 28, "new_price": 27, "changed_at": now + timedelta(days=1)}]
    assert history_checks(rows, 26) == {"chain_continuous": False, "chain_breaks": 1,
        "latest_event_matches_current": False, "timestamps_ordered": True}


@pytest.mark.asyncio
async def test_write_guard_never_executes():
    with pytest.raises(RuntimeError, match="read-only"):
        await forbidden("INSERT INTO private_table VALUES ($1)", "private data")


@pytest.mark.asyncio
async def test_connection_guard_rejects_readwrite():
    class Connection:
        async def fetchval(self, _query):
            return "off"
    with pytest.raises(RuntimeError, match="default_transaction_read_only"):
        await assert_readonly(Connection())


def test_aggregate_does_not_claim_browser_e2e():
    rows = [{"sample": {"id": "123456789"}, "found": True,
             "checks": {"alternative_unique_ids": True, "chain_continuous": False},
             "errors": [], "alternatives": [], "price_history": {"changes": 2}, "photo_count": 0}]
    assert aggregate(rows)["failed_checks"] == [{"id": "123456789", "checks": ["chain_continuous"]}]
    assert aggregate(rows)["found"] == 1
