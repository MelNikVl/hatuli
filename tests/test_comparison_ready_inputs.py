"""No invented amount or automatic scenario without a selected offer/input."""
from datetime import datetime, timezone

import pytest

from bot import public_comparison_routes as routes
from tests.test_public_comparison_routes import market_snapshot, deposit_catalog, daily_picks


@pytest.fixture(autouse=True)
def isolated_snapshots(monkeypatch):
    async def reject(*args, **kwargs):
        raise AssertionError("Public comparison tests cannot connect to PostgreSQL")
    monkeypatch.setattr("asyncpg.connect", reject)
    monkeypatch.setattr("asyncpg.create_pool", reject)
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(routes, "load_market_snapshot", lambda: market_snapshot(now))
    monkeypatch.setattr(routes, "load_deposit_catalog", lambda: deposit_catalog(now))
    monkeypatch.setattr(routes, "load_daily_picks", lambda: daily_picks(now))
    monkeypatch.setattr(routes, "forecast_for_offer", lambda *args, **kwargs: None)


def test_manual_selection_without_input_has_no_demo_amount_or_calculation():
    context = routes.comparison_context(manual=True)
    assert context["selected_offer"] is None
    assert context["form_values"]["price"] == ""
    assert context["calculation"] is None
    assert not context["manual_price_example"]
    assert context["errors"] == []


def test_unknown_listing_reports_input_error_without_calculating_another_price():
    context = routes.comparison_context(listing="9999999999")
    assert context["selected_offer"] is None
    assert context["form_values"]["price"] == ""
    assert context["calculation"] is None
    assert context["errors"]


def test_explicit_manual_price_produces_ready_calculation():
    context = routes.comparison_context(manual=True, overrides={"price": 26_800_000})
    assert context["calculation"]["initial_capital"] == 26_800_000
    assert context["errors"] == []


def test_invalid_explicit_manual_price_keeps_validation_error():
    context = routes.comparison_context(manual=True, overrides={"price": ""})
    assert context["calculation"] is None
    assert context["errors"]


def test_selected_offer_keeps_source_price_and_calculation():
    context = routes.comparison_context(listing="1000000001")
    assert context["selected_offer"]["price"] == 32_000_000
    assert context["calculation"]["initial_capital"] == 32_000_000
    assert context["errors"] == []


@pytest.mark.parametrize("market", ["primary", "secondary"])
def test_unknown_finish_and_build_status_have_no_customer_placeholder(market):
    item = {"id": "newbuild:1" if market == "primary" else "1000000001",
            "market_type": market, "rooms": 2, "area": 60, "price": 32_000_000,
            "finish_label": "Отделка не подтверждена", "finish_level": None,
            "build_status": "unknown", "build_status_label": "Статус ввода не подтверждён"}
    public = routes._public_offer(item)
    assert public["finish_label"] is None
    assert public["build_status_label"] is None
