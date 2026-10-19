"""Selected comparison cards keep public street/house across snapshot editions."""
from datetime import datetime, timezone

import pytest

from bot import public_comparison_routes as routes
from bot.core.comparison_offer_details import build_offer_details
from bot.core.market_comparison import build_market_snapshot


LISTING = "1016000000"


@pytest.fixture(autouse=True)
def isolated_sources(monkeypatch):
    async def reject(*args, **kwargs):
        raise AssertionError("Address projection cannot connect to PostgreSQL")

    monkeypatch.setattr("asyncpg.connect", reject)
    monkeypatch.setattr("asyncpg.create_pool", reject)
    monkeypatch.setattr(routes, "forecast_for_offer", lambda *args, **kwargs: {"status": "insufficient_history"})
    monkeypatch.setattr(routes, "load_deposit_catalog", lambda: {"offers": []})


def context(monkeypatch, selected_address, market_address=None, *, has_detail=True):
    now = datetime.now(timezone.utc)
    picks = {"selection_date": now.date().isoformat(), "generated_at": now.isoformat(),
             "items": [{"id": LISTING, "price": 30_000_000, "area": 60, "rooms": 2,
                        "source_url": f"https://krisha.kz/a/show/{LISTING}",
                        "observed_at": now.isoformat(), "source_checked_at": now.isoformat(),
                        "address": selected_address}]}
    market = build_market_snapshot([], [], now=now)
    if has_detail:
        market["listing_details"] = [{"id": LISTING, "current_price": 30_000_000,
                                      "area": 60, "rooms": 2,
                                      **build_offer_details({"price": 30_000_000, "address": market_address},
                                                            "secondary", now=now)}]
    monkeypatch.setattr(routes, "load_daily_picks", lambda: picks)
    monkeypatch.setattr(routes, "load_market_snapshot", lambda: market)
    return routes.comparison_context(listing=LISTING, scope="city")


@pytest.mark.parametrize("market_address", [None, "пр. Туран 34, телефон +7 707 1234567"])
def test_missing_or_unsafe_market_address_preserves_selected_street_house(monkeypatch, market_address):
    result = context(monkeypatch, "ул. Кунаева 12, кв. 18", market_address)
    assert result["selected_offer"]["address"] == "ул. Кунаева 12"
    assert result["offer_choices"][0]["address"] == "ул. Кунаева 12"
    assert "Адрес из объявления" in result["selected_offer"]["address_note"]
    assert "1234567" not in repr(result["offer_choices"])


def test_market_address_with_flat_is_sanitized_before_selection(monkeypatch):
    result = context(monkeypatch, "ул. Кунаева 12", "пр. Туран 34, квартира 18 — звоните +7 707 1234567")
    assert result["selected_offer"]["address"] == "пр. Туран 34"
    assert result["offer_choices"][0]["address"] == "пр. Туран 34"
    assert "1234567" not in repr(result["offer_choices"])


def test_no_market_detail_still_sanitizes_selected_edition_address(monkeypatch):
    result = context(monkeypatch, "ул. Кунаева 12, кв. 18", has_detail=False)
    assert result["selected_offer"]["address"] == "ул. Кунаева 12"


def test_no_safe_address_is_explicit_instead_of_catalog_guess(monkeypatch):
    result = context(monkeypatch, "ул. Кунаева 12, телефон +7 707 1234567",
                     "пр. Туран 34, seller@example.com")
    assert result["selected_offer"]["address"] is None
    assert result["selected_offer"]["address_note"] == "Точный адрес не указан в источнике."
