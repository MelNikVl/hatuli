"""Public card enrichment must preserve the buyer budget and address privacy."""
from datetime import datetime, timedelta, timezone

import pytest

from bot.core import daily_picks_display as display
from bot.core.apartment_selection_reasons import best_apartment_reasons
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
                floor=5, floors_total=12, price_per_m2=500_000,
                price_comparison={"discount_pct": 16.7, "reference_price_per_m2": 600_000,
                                  "scope": "в том же ЖК", "comparable_count": 8},
                selection_facts={"ceiling_height": 3.0},
                complex_name="Тестовый ЖК", complex_identity={"checked": True, "status": "catalog_bound",
                    "canonical_id": 1, "canonical_name": "Тестовый ЖК", "umbrella_id": None,
                    "umbrella_name": None, "house_id": None, "house_label": None, "binding_method": "name"},
                address="ул. Кунаева 12", observed_at=NOW.isoformat(),
                source_checked_at=NOW.isoformat(), source_status="active")
    item["reasons"] = best_apartment_reasons(item, NOW)
    return dict(selection_date="2026-10-13", generated_at=NOW.isoformat(),
                source_observed_at=NOW.isoformat(), items=[item])


def market_detail(**changes):
    row = {"price": changes.get("current_price", 30_000_000), "area": 60, "address": None, "ceiling_height": 3.0}
    return {"id": "1016000000", "current_price": 30_000_000, "area": 60, "rooms": 2,
            "floor": 5, "floors_total": 12,
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


@pytest.mark.parametrize("price", [29_000_000, 31_000_000, 100_000_000])
def test_changed_scanner_price_hides_card_even_when_still_within_budget(monkeypatch, price):
    details = market_detail(current_price=price)
    monkeypatch.setattr(display, "load_market_snapshot", lambda: {"offers": [], "listing_details": [details]})
    data = snapshot()
    assert display.prepare_daily_picks(data, NOW)["items"] == []
    assert data["items"][0]["price"] == 30_000_000


def test_exact_budget_with_unchanged_source_and_three_factors_is_kept(monkeypatch):
    data = snapshot()
    selected = data["items"][0]
    selected.update(price=100_000_000, price_per_m2=1_666_667,
                    price_comparison={"discount_pct": 16.7, "reference_price_per_m2": 2_000_000,
                                      "scope": "в том же ЖК", "comparable_count": 8})
    selected["reasons"] = best_apartment_reasons(selected, NOW)
    monkeypatch.setattr(display, "load_market_snapshot", lambda: {"offers": [], "listing_details": [market_detail(current_price=100_000_000)]})
    item = display.prepare_daily_picks(data, NOW)["items"][0]
    assert item["price"] == 100_000_000
    assert len(item["reasons"]) == 3
    assert item["source_checked_at"] == NOW.isoformat()


def test_card_with_less_than_three_supported_factors_is_hidden():
    data = snapshot()
    data["items"][0]["selection_facts"]["ceiling_height"] = None
    # The old rendered text must not supply a missing structured fact.
    assert display.prepare_daily_picks(data, NOW)["items"] == []


@pytest.mark.parametrize("changes", [{"area": 75}, {"rooms": 3}])
def test_changed_source_characteristics_hide_card(monkeypatch, changes):
    monkeypatch.setattr(display, "load_market_snapshot", lambda: {"offers": [], "listing_details": [market_detail(**changes)]})
    assert display.prepare_daily_picks(snapshot(), NOW)["items"] == []


def test_actual_history_gap_stays_with_chart_without_promoting_generic_checklists(monkeypatch):
    history = build_offer_details({"price": 30_000_000, "price_events": [
        {"old_price": 32_000_000, "new_price": 31_000_000, "changed_at": NOW-timedelta(days=2)},
        {"old_price": 30_500_000, "new_price": 30_000_000, "changed_at": NOW-timedelta(days=1)},
    ]}, "secondary", now=NOW)["price_history"]
    monkeypatch.setattr(display, "load_market_snapshot", lambda: {"offers": [], "listing_details": [market_detail(price_history=history)]})
    data = snapshot()
    data["items"][0]["cautions"] = ["GENERIC-CHECKLIST-MUST-NOT-BECOME-A-NOTICE"]
    item = display.prepare_daily_picks(data, NOW)["items"][0]
    assert len(item["reasons"]) == 3
    assert "пропуски" in item["price_history"]["warning"]
    assert item["evidence_notices"] == []
