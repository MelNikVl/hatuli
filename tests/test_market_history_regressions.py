"""Regressions for dated exits and relists, without a running database."""
from datetime import datetime, timedelta, timezone

import pytest

from bot.core.complex_market_profile import _build_liquidity, _build_price
from bot.core.listing_activity import active_at, active_intervals, confirmed_relists
from bot.core.property_timeline import _build_events, _compute_metrics


BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def day(n):
    return BASE + timedelta(days=n)


def listing(lid="a", start=0, end=None, pid=1, history=(), as_of=100, **kwargs):
    row = dict(listing_id=lid, property_id=pid, first_seen=day(start),
               last_seen=day(end if end is not None else as_of),
               archived_at=day(end) if end is not None else None,
               is_active=end is None, archive_history=list(history),
               price=20_000_000, area=50, rooms=2)
    row.update(kwargs)
    row["active_at_as_of"] = active_at(row, row["archive_history"], day(as_of))
    return row


def gap(start, end):
    return dict(archived_at=day(start), reactivated_at=day(end))


@pytest.mark.parametrize("archive_day,expected", [(20, 1), (30, 1), (31, 0), (80, 0)])
def test_30_day_exit_uses_event_date(archive_day, expected):
    rows = [listing(str(i), end=archive_day, pid=i) for i in range(5)]
    result = _build_liquidity(rows, day(100))
    assert result["fraction_disappearing"]["within_30d"]["fraction"] == expected


def test_past_exit_is_retained_after_reactivation():
    rows = [listing(str(i), pid=i, history=[gap(20, 40)]) for i in range(5)]
    result = _build_liquidity(rows, day(100))
    assert result["fraction_disappearing"]["within_30d"]["fraction"] == 1
    assert result["median_observed_dom_days"] == 80
    assert result["true_relist_count"] == 0  # Same ID returned, not a new ad.


def test_one_archived_ad_does_not_mean_property_exit():
    rows = [row for i in range(5)
            for row in (listing(f"{i}a", end=20, pid=i), listing(f"{i}b", start=10, pid=i))]
    result = _build_liquidity(rows, day(100))
    assert result["fraction_disappearing"]["within_30d"]["fraction"] == 0
    assert result["true_relist_count"] == 0
    assert result["median_observed_dom_days"] == 100


def test_historical_cutoff_inside_closed_archive_gap():
    rows = [listing(str(i), pid=i, end=80, history=[gap(10, 40)], as_of=20) for i in range(5)]
    assert active_intervals(rows[0], rows[0]["archive_history"], day(20)) == [(day(0), day(10))]
    result = _build_liquidity(rows, day(20))
    assert result["median_observed_dom_days"] == 10
    assert result["active_property_count_for_stale"] == 0
    assert result["fraction_disappearing"]["within_30d"]["sample_size"] == 0


def test_archive_after_cutoff_does_not_change_historical_activity():
    row = listing(end=80, as_of=30)
    assert row["active_at_as_of"]
    assert active_intervals(row, [], day(30)) == [(day(0), day(30))]


def test_reactivation_timestamp_is_active_boundary():
    row = listing(history=[gap(10, 20)])
    assert not active_at(row, row["archive_history"], day(19))
    assert active_at(row, row["archive_history"], day(20))


def test_parallel_and_relisted_ids_share_one_definition_with_timeline():
    rows = [listing("a", end=20), listing("b", start=10, end=25), listing("c", start=30)]
    histories = {r["listing_id"]: [] for r in rows}
    assert confirmed_relists(rows, histories) == {"c": "b"}
    assert _compute_metrics(rows, {}, histories)["relist_count"] == 1
    events = _build_events(rows, {}, histories, [])
    assert [e["listing_id"] for e in events if e["type"] == "listing_relist"] == ["c"]
    assert _build_liquidity(rows, day(100))["true_relist_count"] == 1


def test_old_last_seen_is_not_proof_of_archive():
    rows = [listing("a", last_seen=day(5)), listing("b", start=10)]
    assert confirmed_relists(rows, {}) == {}
    assert _compute_metrics(rows, {}, {})["relist_count"] == 0


def test_simultaneous_new_ids_count_one_new_episode():
    rows = [listing("a", end=20), listing("b", start=30), listing("c", start=30)]
    assert confirmed_relists(rows, {}) == {"b": "a"}


def test_continuous_parallel_id_blocks_false_relist():
    rows = [listing("a", end=10), listing("b", start=5), listing("c", start=20)]
    assert confirmed_relists(rows, {}) == {}


def test_price_has_one_vote_per_property():
    rows = [listing(str(i), pid=i) for i in range(5)]
    # Duplicating one property's ad many times must not shift the median.
    rows.extend(listing(f"dup{i}", start=10, pid=0, price=100_000_000) for i in range(20))
    result = _build_price(rows)
    assert result["sample_size"] == 5
    assert result["median_asking_price"] == 20_000_000


def test_exit_followed_by_new_id_remains_in_first_30_day_window():
    rows = [r for i in range(5)
            for r in (listing(f"{i}a", end=10, pid=i), listing(f"{i}b", start=40, pid=i))]
    result = _build_liquidity(rows, day(100))
    assert result["fraction_disappearing"]["within_30d"]["fraction"] == 1
    assert result["true_relist_count"] == 5


def test_seller_change_keeps_previous_id_for_parallel_ads():
    rows = [listing("a", seller_name="Иван"), listing("b", start=5, seller_name="Пётр")]
    events = _build_events(rows, {}, {}, [])
    changes = [e for e in events if e["type"] == "seller_observed_change"]
    assert len(changes) == 1
    assert changes[0]["evidence"]["previous_listing_id"] == "a"
    assert not any(e["type"] == "listing_relist" for e in events)
