"""Public reasons/financial thresholds use snapshots, never request-time DBs."""
import copy
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from bot.admin_web import create_admin_app
from bot.core.comparison_offer_details import build_offer_details
from bot.core.daily_picks_display import prepare_daily_picks
from tests.test_daily_picks_public_page import snapshot as daily_snapshot
from tests.test_public_comparison_routes import NoPrivateDB, market_snapshot, deposit_catalog, daily_picks


NOW = datetime.now(timezone.utc)


def detailed_market(now, identifier="1000000001", price=32_000_000, area=60):
    market = market_snapshot(now)
    detail = market["listing_details"][0]
    detail.update(id=identifier, area=area, current_price=price)
    detail.update(build_offer_details({"price": price, "area": area, "address": "Сыганак 12/1",
        "ceiling_height": 3, "kitchen_area": 15,
        "price_events": [{"old_price": price+2_000_000, "new_price": price+1_000_000, "changed_at": now-timedelta(days=20)},
                         {"old_price": price+1_000_000, "new_price": price, "changed_at": now-timedelta(days=3)}]}, "secondary", now=now))
    return market


def full_deposits(now):
    catalog = deposit_catalog(now)
    annual = catalog["offers"][0]
    catalog["offers"].append({**annual, "id": "freedom-strategy-3", "name": "Стратегия", "term_months": 3,
                             "effective_pct": 19.5, "nominal_pct": 18.09})
    return catalog


@pytest.fixture
def public_app(monkeypatch):
    async def reject(*args, **kwargs):
        raise AssertionError("Public selection display must not read PostgreSQL")
    monkeypatch.setattr("asyncpg.connect", reject)
    monkeypatch.setattr("asyncpg.create_pool", reject)
    monkeypatch.setattr("bot.public_comparison_routes.load_market_snapshot", lambda: detailed_market(NOW))
    monkeypatch.setattr("bot.public_comparison_routes.load_deposit_catalog", lambda: full_deposits(NOW))
    def selection():
        picks = daily_picks(NOW)
        picks["items"][0]["price_comparison"] = {"discount_pct": 14.7, "reference_price_per_m2": 625_000,
                                                "scope": "в том же ЖК", "comparable_count": 6}
        return picks
    monkeypatch.setattr("bot.public_comparison_routes.load_daily_picks", selection)
    return create_admin_app(NoPrivateDB(), "test-form-secret", "test")


@pytest.mark.asyncio
async def test_compare_why_shows_price_cash_gap_ceiling_kitchen_and_distinct_cuts(public_app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=public_app), base_url="http://test") as client:
        response = await client.get("/compare?listing=1000000001&scope=city")
    assert response.status_code == 200
    assert "Почему в подборке" in response.text
    assert "14,7% ниже" in response.text and "5,5 млн" in response.text
    assert "цену снижали 2 раза" not in response.text
    assert response.text.count('data-price-history-chart') == 1  # selected offer has the recorded history
    assert "2 000 000 ₸" in response.text and "Все изменения по датам" in response.text
    assert "Высокие потолки 3 м" in response.text and "Кухня 15 м²" in response.text
    assert "6 аналогов:" not in response.text
    assert "19,50%" in response.text and "3 мес." in response.text and "продлении ставка может измениться" in response.text
    assert "PRIVATE" not in response.text


@pytest.mark.asyncio
async def test_no_js_rental_calculator_compares_income_to_short_maximum_without_growth(public_app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=public_app), base_url="http://test") as client:
        response = await client.post("/compare/calculate", data={"listing": "1000000001", "price": 32_000_000,
            "mode": "rent_out", "monthly_rent": 500_000, "annual_growth_pct": 50,
            "repair_cost": 0, "buy_cost": 0, "annual_owner_cost": 0, "vacancy_months": 0, "wait_months": 0})
    assert response.status_code == 200
    assert "Доход от аренды после расходов" in response.text
    assert "18,8% за первый год" in response.text
    assert "выше 520 000" in response.text
    assert "В вашем сценарии доходность аренды выше этой ставки." not in response.text
    assert "3 мес." in response.text


def test_daily_enrichment_keeps_verified_publication_and_uses_current_recorded_facts(monkeypatch):
    original = daily_snapshot(NOW)
    frozen = copy.deepcopy(original)
    item = original["items"][0]
    market = detailed_market(NOW, item["id"], item["price"], item["area"])
    monkeypatch.setattr("bot.core.daily_picks_display.load_market_snapshot", lambda: market)
    displayed = prepare_daily_picks(original, now=NOW)
    assert original == frozen
    for field in ("generated_at", "selection_date", "source_observed_at"):
        assert displayed[field] == original[field]
    actual = displayed["items"][0]
    assert actual["rank"] == item["rank"] and actual["source_checked_at"] == item["source_checked_at"]
    assert any("потолки" in reason for reason in actual["reasons"])
    assert any("Кухня" in reason for reason in actual["reasons"])
    assert not any("2 раза" in reason for reason in actual["reasons"])
    assert actual["price_history"]["changes_count"] == 2
    assert not any("аналогов:" in reason or "±15" in reason for reason in actual["reasons"])


def test_newer_cache_price_mismatch_suppresses_discount_and_adds_caution(monkeypatch):
    original = daily_snapshot(NOW)
    item = original["items"][0]
    market = detailed_market(NOW, item["id"], item["price"]+1_000_000, item["area"])
    monkeypatch.setattr("bot.core.daily_picks_display.load_market_snapshot", lambda: market)
    actual = prepare_daily_picks(original, now=NOW)["items"][0]
    assert actual["price_comparison"] is None
    assert not any("ниже похожих" in reason or "снижение" in reason.lower() for reason in actual["reasons"])
    assert any("Цена в подборке отличается" in caution for caution in actual["cautions"])


def test_changed_area_and_large_kitchen_never_break_daily_page_or_promote_old_card(monkeypatch):
    original = daily_snapshot(NOW)
    item = original["items"][0]
    market = detailed_market(NOW, item["id"], item["price"], area=100)
    market["listing_details"][0]["selection_facts"]["kitchen_area"] = 50
    monkeypatch.setattr("bot.core.daily_picks_display.load_market_snapshot", lambda: market)
    actual = prepare_daily_picks(original, now=NOW)["items"][0]
    assert actual["area"] == 50 and actual["price_comparison"] is None
    assert actual["selection_facts"]["kitchen_area"] is None
    assert not actual["reasons"]
    assert actual["price_history"]["status"] == "incomplete"
    assert any("Характеристики объявления изменились" in caution for caution in actual["cautions"])


@pytest.mark.asyncio
async def test_changed_area_metadata_does_not_break_public_comparison(public_app, monkeypatch):
    market = detailed_market(NOW, area=100)
    market["listing_details"][0]["selection_facts"]["kitchen_area"] = 50
    monkeypatch.setattr("bot.public_comparison_routes.load_market_snapshot", lambda: market)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=public_app), base_url="http://test") as client:
        response = await client.get("/compare?listing=1000000001")
    assert response.status_code == 200
    assert "Кухня 50" not in response.text
    assert "Характеристики объявления изменились" in response.text
