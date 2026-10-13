"""Asking-price history regressions; no database, migrations or ingestion."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from bot.core.buyer import summarize_price_history
from bot.core.buyer_price_chart import render_price_chart
from bot.core.price_history_integrity import inspect_price_history, valid_price
from bot.core.property_timeline import _build_events, _compute_metrics, _price_trajectory


BASE = datetime(2026, 10, 1, tzinfo=timezone.utc)


def event(old, new, day=1):
    return {"old_price": old, "new_price": new,
            "changed_at": BASE + timedelta(days=day), "at": f"{day + 1:02}.10.2026"}


@pytest.mark.parametrize("value", [None, 0, -1, True, "100", float("nan"), float("inf"), Decimal("NaN")])
def test_invalid_prices_are_not_historical_values(value):
    assert not valid_price(value)
    result = inspect_price_history([event(100, value)], 90)
    assert result["status"] == "incomplete"
    assert not result["can_compute_net_change"]


def test_chain_gap_has_both_values_and_observation_dates():
    result = inspect_price_history([event(100, 90, 1), event(80, 70, 3)], 70)
    assert result["status"] == "incomplete"
    assert result["latest_matches_current"] is True
    gap = result["issues"][0]
    assert gap["code"] == "price_chain_gap"
    assert gap["previous_new_price"] == 90 and gap["next_old_price"] == 80
    assert gap["previous_at"] == (BASE + timedelta(days=1)).isoformat()
    assert gap["at"] == (BASE + timedelta(days=3)).isoformat()
    assert not result["can_compute_net_change"]


def test_repeated_real_ingestion_event_is_not_counted_as_a_continuous_second_cut():
    # Read-only control from listing 1014031167: the same 78 -> 75 million
    # change was stored twice a millisecond apart. Do not silently connect it.
    events = [{"old_price": 78_000_000, "new_price": 75_000_000,
               "changed_at": datetime.fromisoformat(at)} for at in (
                   "2026-07-23T15:09:33.911941+00:00", "2026-07-23T15:09:33.913029+00:00")]
    result = inspect_price_history(events, 75_000_000)
    assert result["status"] == "incomplete"
    assert result["issues"][0]["previous_new_price"] == 75_000_000
    assert result["issues"][0]["next_old_price"] == 78_000_000
    assert not result["can_compute_net_change"]


def test_current_price_mismatch_is_separate_from_continuous_event_chain():
    result = inspect_price_history([event(100, 90), event(90, 80, 2)], 75, BASE + timedelta(days=5))
    assert result["chain_continuous"]
    assert result["latest_matches_current"] is False
    issue = result["issues"][0]
    assert issue["code"] == "current_price_mismatch"
    assert issue["latest_event_price"] == 80 and issue["current_price"] == 75
    assert issue["current_at"] == (BASE + timedelta(days=5)).isoformat()
    assert not result["can_compute_net_change"]


def test_no_events_do_not_prove_unchanged_price_since_first_seen():
    result = inspect_price_history([], 75)
    assert result["status"] == "no_changes_recorded"
    assert result["latest_matches_current"] is None
    assert not result["can_compute_net_change"]


@pytest.mark.parametrize("current", [0, float("nan"), True])
def test_invalid_current_price_blocks_a_total_even_with_valid_events(current):
    result = inspect_price_history([event(100, 90)], current)
    assert result["status"] == "incomplete"
    assert result["issues"][0]["code"] == "invalid_current_price"
    assert not result["can_compute_net_change"]


def test_zero_net_change_is_valid_when_intermediate_changes_are_continuous():
    result = summarize_price_history({"events": [event(100, 90), event(90, 100, 2)], "current": 100})
    assert result["integrity_status"] == "consistent"
    assert result["net_change"] == 0 and result["changes"] == 2


@pytest.mark.parametrize("events,current", [
    ([event(100, 90), event(80, 70, 2)], 70),
    ([event(100, 90)], 80),
    ([event(100, 90), event(90, None, 2)], 80),
])
def test_buyer_preserves_valid_events_but_never_connects_incomplete_history(events, current):
    result = summarize_price_history({"events": events, "current": current})
    assert result["integrity_status"] == "incomplete" and result["history_warning"]
    assert result["net_change"] is None
    assert render_price_chart(result) is None


@pytest.mark.asyncio
async def test_builder_uses_stable_event_order_and_does_not_backdate_current_price(monkeypatch):
    from bot.core.listing_detail import build_price_history
    from bot.db import pg

    fetch = AsyncMock(return_value=[event(100, 90), event(90, 80, 3)])
    monkeypatch.setattr(pg, "fetch", fetch)
    monkeypatch.setattr(pg, "fetchrow", AsyncMock(return_value={
        "price": 80, "first_seen": BASE - timedelta(days=30), "last_seen": BASE + timedelta(days=4)}))
    result = await build_price_history("12345")
    assert "changed_at ASC, id ASC" in fetch.await_args.args[0]
    assert [p["price"] for p in result["points"]] == [100, 90, 80]
    assert result["points"][0]["at"] == "02.10.2026"
    assert result["points"][0]["basis"] == "previous_price_at_change"
    assert result["net_change"] == -20


@pytest.mark.asyncio
async def test_builder_keeps_raw_events_and_hides_discontinuous_chart(monkeypatch):
    from bot.core.listing_detail import build_price_history
    from bot.db import pg

    monkeypatch.setattr(pg, "fetch", AsyncMock(return_value=[event(100, 90), event(80, 70, 3)]))
    monkeypatch.setattr(pg, "fetchrow", AsyncMock(return_value={
        "price": 65, "first_seen": BASE, "last_seen": BASE + timedelta(days=4)}))
    result = await build_price_history("12345")
    assert len(result["events"]) == 2 and result["changes"] == 2
    assert result["points"] == [] and result["net_change"] is None
    assert {i["code"] for i in result["integrity"]["issues"]} == {"price_chain_gap", "current_price_mismatch"}


@pytest.mark.asyncio
async def test_builder_no_changes_is_not_a_current_price_backfilled_to_first_seen(monkeypatch):
    from bot.core.listing_detail import build_price_history
    from bot.db import pg

    monkeypatch.setattr(pg, "fetch", AsyncMock(return_value=[]))
    monkeypatch.setattr(pg, "fetchrow", AsyncMock(return_value={
        "price": 65, "first_seen": BASE, "last_seen": BASE + timedelta(days=4)}))
    result = await build_price_history("12345")
    assert result["current"] == 65 and result["current_at"]
    assert result["points"] == [] and result["net_change"] is None
    assert result["integrity_status"] == "no_changes_recorded"


def timeline_listing(price=70):
    return {"listing_id": "12345", "property_id": 1, "first_seen": BASE,
            "last_seen": BASE + timedelta(days=5), "price": price,
            "is_active": True, "linked_at": BASE}


def test_property_timeline_does_not_claim_current_price_on_first_seen_date():
    listing = timeline_listing()
    events = _build_events([listing], {}, {}, [])
    first = next(e for e in events if e["type"] == "listing_first_seen")
    assert first["after"]["price"] is None and first["evidence"]["price_note"]
    trajectory = _price_trajectory([listing], {"12345": [event(100, 90), event(80, 70, 3)]})
    assert min(at for at, _ in trajectory) > BASE
    assert 80 in [price for _, price in trajectory]  # A gap's old price is retained as evidence.


def test_property_timeline_flags_gap_and_reports_current_cache_without_false_net_change():
    result = _compute_metrics([timeline_listing(65)],
                              {"12345": [event(100, 90), event(80, 70, 3)]}, {})
    assert result["latest_price"] == 65
    assert result["total_price_change_pct"] is None
    assert not result["price_history_continuous"]
    assert result["price_history_integrity"]["12345"]["status"] == "incomplete"


def test_property_timeline_no_changes_keeps_cache_distinct_from_dated_history():
    result = _compute_metrics([timeline_listing()], {}, {})
    assert result["latest_price"] == 70
    assert result["total_price_change_pct"] is None
    assert result["price_history_start_at"] != result["first_seen_at"]


def test_telegram_keeps_events_and_explains_gap_without_zero_change_summary():
    from bot.buyer.telegram import price_history_lines, render_history

    history = summarize_price_history({"events": [event(100, 90), event(80, 70, 3)], "current": 70})
    lines = price_history_lines(history)
    assert any("пропуски" in line for line in lines)
    assert not any("без итогового изменения" in line or "за записанную историю:" in line for line in lines)
    text, _ = render_history({"listing": {"id": "12345"}, "price_history": history}, 0)
    assert text.count("→") == 2 and "пропуски" in text
