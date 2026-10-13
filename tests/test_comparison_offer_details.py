"""Source address/event semantics; isolated, with no DB or HTTP calls."""
from datetime import datetime, timedelta, timezone
import json

import pytest

from bot.core.comparison_offer_details import (
    build_offer_details, details_for_selected_offer, price_history, validate_offer_details,
)
from bot.core.market_comparison import build_market_snapshot, load_market_snapshot, publish_market_snapshot
from tests.test_market_comparison import primary, secondary, NOW


def event(old, new, day=1):
    return {"changed_at": NOW - timedelta(days=day), "old_price": old, "new_price": new,
            "private_contact": "DO-NOT-COPY"}


def test_all_recorded_changes_with_dates_directions_and_no_private_tail():
    row = {"price": 41_000_000, "address": "Есильский р-н, Айнаколь 62 — private contact and ad text",
           "phone": "DO-NOT-COPY", "price_events": [event(43_000_000, 42_000_000, 40), event(42_000_000, 41_000_000)]}
    details = build_offer_details(row, "secondary", now=NOW)
    assert details["address"] == "Есильский р-н, Айнаколь 62"
    history = details["price_history"]
    assert history["status"] == "consistent" and history["changes_count"] == 2
    assert history["net_change"] == -2_000_000
    assert [e["direction"] for e in history["events"]] == ["down", "down"]
    assert history["events"][-1]["delta"] == -1_000_000
    assert history["events"][-1]["date_label"] == "12.10.2026, 10:00"
    assert "private" not in json.dumps(details) and "DO-NOT-COPY" not in json.dumps(details)


@pytest.mark.parametrize("events,current", [
    ([event(44_000_000, 43_500_000, 2), event(44_000_000, 43_500_000)], 43_500_000),
    ([event(43_000_000, 42_000_000)], 41_000_000),
])
def test_gap_or_current_mismatch_preserves_each_event_without_net(events, current):
    details = build_offer_details({"price": current, "price_events": events}, "secondary", now=NOW)
    history = details["price_history"]
    assert history["status"] == "incomplete" and len(history["events"]) == len(events)
    assert history["net_change"] is history["net_pct"] is None and history["warning"]


def test_primary_first_change_has_unknown_previous_price_and_keeps_building():
    row = {"price": 30_000_000, "address": "Ұлы дала, участок 18", "building": "C", "section": "1",
           "price_events": [{"price": 30_000_000, "changed_at": NOW - timedelta(days=2)}]}
    details = build_offer_details(row, "primary", now=NOW)
    history = details["price_history"]
    assert history["status"] == "consistent" and history["changes_count"] == 1
    assert history["events"][0]["old_price"] is None
    assert history["events"][0]["delta"] is None and history["net_change"] is None
    assert "не сохранена" in history["warning"]
    assert details["building"] == "C" and details["section"] == "1"
    assert "Адрес проекта" in details["address_note"]
    row["price"] = 32_000_000
    row["price_events"].append({"price": 32_000_000, "changed_at": NOW - timedelta(days=1)})
    history = build_offer_details(row, "primary", now=NOW)["price_history"]
    assert history["events"][-1]["old_price"] == 30_000_000
    assert history["net_change"] == 2_000_000
    assert history["events"][-1]["direction"] == "up"


def test_primary_single_event_mismatch_is_not_a_complete_history():
    details = build_offer_details({"price": 31_000_000, "price_events": [{"price": 30_000_000, "changed_at": NOW}]}, "primary", now=NOW)
    assert details["price_history"]["status"] == "incomplete"
    assert details["price_history"]["net_change"] is None


def test_none_recorded_is_distinct_from_unavailable_or_price_never_changed():
    details = build_offer_details({"price": 30_000_000}, "secondary", now=NOW)
    assert details["address"] is None
    assert details["price_history"]["status"] == "no_changes_recorded"
    assert not details["price_history"]["events"]
    assert details_for_selected_offer(None, 30_000_000)["price_history"]["status"] == "unavailable"


@pytest.mark.parametrize("address", ["", "— description tail", "– description tail", "\n", "a" * 501])
def test_unusable_source_address_does_not_abort_the_market(address):
    assert build_offer_details({"price": 30_000_000, "address": address}, "secondary", now=NOW)["address"] is None


def test_daily_price_disagreement_uses_display_price_and_suppresses_net():
    details = {"current_price": 41_000_000,
               **build_offer_details({"price": 41_000_000, "price_events": [event(42_000_000, 41_000_000)]}, "secondary", now=NOW)}
    actual = details_for_selected_offer(details, 40_000_000)
    assert actual["price_history"]["status"] == "incomplete"
    assert actual["price_history"]["net_change"] is None
    assert "подборке" in actual["price_history"]["warning"]
    assert details["price_history"]["status"] == "consistent"


def test_public_nested_projection_recomputes_forged_integrity_and_strips_contact():
    details = build_offer_details({"price": 41_000_000, "price_events": [event(42_000_000, 41_000_000)]}, "secondary", now=NOW)
    details.update(phone="DO-NOT-COPY", address_note="DO-NOT-COPY")
    details["price_history"].update(status="consistent", warning="DO-NOT-COPY", net_change=999999)
    details["price_history"]["events"][0].update(phone="DO-NOT-COPY", date_label="DO-NOT-COPY")
    safe = validate_offer_details(details, "secondary", 40_000_000, now=NOW)
    assert safe["price_history"]["status"] == "incomplete"
    assert "DO-NOT-COPY" not in json.dumps(safe)


def test_supplemental_public_listing_keeps_metadata_outside_market_filters(tmp_path):
    row = secondary(100777, is_duplicate=True, lat=None, comparison_only=True, public_comparison_listing=True,
                    address="Айнаколь 62", price_events=[event(37_000_000, 36_000_000)])
    data = build_market_snapshot([primary()], [secondary(), row], now=NOW)
    assert data["summary"]["secondary_count"] == 1
    assert data["listing_details"][0]["id"] == "100777"
    assert data["listing_details"][0]["price_history"]["changes_count"] == 1
    data["listing_details"][0]["phone"] = "DO-NOT-COPY"
    path = tmp_path / "market.json"
    publish_market_snapshot(data, path)
    assert len(load_market_snapshot(path)["listing_details"]) == 1
    assert "DO-NOT-COPY" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("bad", [True, float("nan"), float("inf"), 0, -1, None])
def test_invalid_event_price_fails_closed(bad):
    with pytest.raises(ValueError):
        price_history([{"at": NOW, "old_price": 30_000_000, "new_price": bad}], 30_000_000, now=NOW)


def test_future_or_out_of_order_events_fail_closed():
    with pytest.raises(ValueError):
        price_history([{"at": NOW + timedelta(seconds=1), "old_price": 30_000_000, "new_price": 31_000_000}], 31_000_000, now=NOW)
    with pytest.raises(ValueError):
        price_history([{"at": NOW, "old_price": 30_000_000, "new_price": 31_000_000},
                       {"at": NOW - timedelta(seconds=1), "old_price": 31_000_000, "new_price": 32_000_000}], 32_000_000, now=NOW)
