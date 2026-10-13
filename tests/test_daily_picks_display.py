"""Public card enrichment must preserve the buyer budget and address privacy."""
from datetime import datetime, timezone

import pytest

from bot.core import daily_picks_display as display
from bot.core.comparison_offer_details import build_offer_details


NOW = datetime(2026, 10, 13, 4, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def no_database_or_forecast_io(monkeypatch):
    async def reject(*args, **kwargs):
        raise AssertionError("Display tests cannot connect to PostgreSQL")

    monkeypatch.setattr("asyncpg.connect", reject)
    monkeypatch.setattr("asyncpg.create_pool", reject)
    monkeypatch.setattr(display, "forecast_for_offer", lambda *args, **kwargs: {"status": "insufficient_history"})
    monkeypatch.setattr(display, "load_market_snapshot", lambda: {"offers": [], "listing_details": []})


def snapshot():
    item = dict(id="1016000000", rank=1, price=30_000_000, area=60, rooms=2,
                address="ул. Кунаева 12", observed_at=NOW.isoformat(),
                source_checked_at=NOW.isoformat(), source_status="active")
    return dict(selection_date="2026-10-13", generated_at=NOW.isoformat(),
                source_observed_at=NOW.isoformat(), items=[item])


def market_detail(**changes):
    row = {"price": 30_000_000, "address": None}
    return {"id": "1016000000", "current_price": 30_000_000, "area": 60, "rooms": 2,
            **build_offer_details(row, "secondary", now=NOW), **changes}


def test_legacy_unvalidated_expensive_card_is_hidden_and_ranks_are_dense():
    data = snapshot()
    data["items"] = [{**data["items"][0], "id": "1016000001", "price": 120_000_000},
                     {**data["items"][0], "rank": 2}]
    result = display.prepare_daily_picks(data, NOW)
    assert [item["id"] for item in result["items"]] == ["1016000000"]
    assert result["items"][0]["rank"] == 1


def test_card_is_hidden_when_later_scanner_price_exceeds_budget(monkeypatch):
    details = market_detail(current_price=100_000_001)
    monkeypatch.setattr(display, "load_market_snapshot", lambda: {"offers": [], "listing_details": [details]})
    assert display.prepare_daily_picks(snapshot(), NOW)["items"] == []


def test_market_without_address_preserves_sanitized_selected_address(monkeypatch):
    monkeypatch.setattr(display, "load_market_snapshot", lambda: {"offers": [], "listing_details": [market_detail()]})
    item = display.prepare_daily_picks(snapshot(), NOW)["items"][0]
    assert item["address"] == "ул. Кунаева 12"
    assert "Адрес из объявления" in item["address_note"]


def test_market_enrichment_cannot_reintroduce_flat_or_contact(monkeypatch):
    details = market_detail(address="пр. Туран 34, квартира 18 — звоните +7 707 1234567")
    monkeypatch.setattr(display, "load_market_snapshot", lambda: {"offers": [], "listing_details": [details]})
    item = display.prepare_daily_picks(snapshot(), NOW)["items"][0]
    assert item["address"] == "пр. Туран 34"
    assert "18" not in item["address"] and "1234567" not in item["address"]


def test_unsafe_market_address_falls_back_to_sanitized_selected_address(monkeypatch):
    details = market_detail(address="пр. Туран 34, телефон +7 707 1234567")
    monkeypatch.setattr(display, "load_market_snapshot", lambda: {"offers": [], "listing_details": [details]})
    data = snapshot()
    data["items"][0]["address"] = "ул. Кунаева 12, кв. 18"
    item = display.prepare_daily_picks(data, NOW)["items"][0]
    assert item["address"] == "ул. Кунаева 12"
    assert "Адрес из объявления" in item["address_note"]
    assert "18" not in item["address"] and "1234567" not in item["address"]


def test_market_enrichment_at_exact_budget_keeps_verified_asking_price(monkeypatch):
    details = market_detail(current_price=100_000_000)
    monkeypatch.setattr(display, "load_market_snapshot", lambda: {"offers": [], "listing_details": [details]})
    data = snapshot()
    item = display.prepare_daily_picks(data, NOW)["items"][0]
    assert item["price"] == 30_000_000
    assert item["price_comparison"] is None
    assert item["source_checked_at"] == NOW.isoformat()
    assert data["items"][0]["price"] == 30_000_000
