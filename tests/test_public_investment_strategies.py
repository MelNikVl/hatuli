"""Anonymous scenario UI: public files only, no DB, HTTP source, or examples."""
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import json

import httpx
import pytest

from bot import public_comparison_routes as routes
from bot.admin_web import create_admin_app
from tests.test_public_comparison_routes import NoPrivateDB, daily_picks, deposit_catalog, market_snapshot


class Inputs(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.inputs = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and attrs.get("name"):
            self.inputs[attrs["name"]] = attrs


@pytest.fixture
def isolated_app(monkeypatch):
    async def deny(*args, **kwargs):
        raise AssertionError("Investment strategies cannot touch PostgreSQL")
    monkeypatch.setattr("asyncpg.connect", deny)
    monkeypatch.setattr("asyncpg.create_pool", deny)
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(routes, "load_market_snapshot", lambda: market_snapshot(now))
    monkeypatch.setattr(routes, "load_deposit_catalog", lambda: deposit_catalog(now))
    monkeypatch.setattr(routes, "load_daily_picks", lambda: daily_picks(now))
    monkeypatch.setattr(routes, "forecast_for_offer", lambda *args, **kwargs: None)
    return create_admin_app(NoPrivateDB(), "isolated-strategy-form-key", "test")


def monthly(**overrides):
    return {"purchase_price": 30_000_000, "repair_cost": 2_000_000,
            "buy_cost": 300_000, "monthly_rent": 250_000, "vacancy_months": 1,
            "annual_owner_cost": 240_000, "wait_months": 2,
            "exit_value": 31_000_000, "sell_cost_pct": 2, **overrides}


def daily(**overrides):
    return {"purchase_price": 30_000_000, "repair_cost": 2_000_000,
            "buy_cost": 300_000, "adr": 25_000, "occupancy_pct": 60,
            "platform_fee_pct": 15.5, "management_fee_pct": 10,
            "cleaning_cost_per_stay": 5_000, "stays_per_month": 5,
            "annual_owner_cost": 600_000, "wait_months": 2,
            "exit_value": 31_000_000, "sell_cost_pct": 2, **overrides}


def construction(**overrides):
    return {"entry_price": 25_000_000, "finishing_cost": 3_000_000,
            "buy_cost": 300_000, "annual_owner_cost": 120_000,
            "wait_months": 18, "exit_value": 31_000_000, "sell_cost_pct": 2, **overrides}


@pytest.mark.asyncio
async def test_initial_page_has_opt_in_inputs_no_scenario_result_or_invented_facts(isolated_app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=isolated_app), base_url="http://test") as client:
        response = await client.get("/compare?listing=1000000001&hex=0%3A0")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert 'id="strategies"' in response.text
    assert 'data-strategy-result="' not in response.text
    assert 'data-strategy-ready' not in response.text
    fields = Inputs(response.text).inputs
    assert fields["budget"]["value"] == "32000000"
    assert fields["monthly__purchase_price"]["value"] == "32000000"
    for name in ["monthly__monthly_rent", "daily__adr", "daily__occupancy_pct", "daily__platform_fee_pct",
                 "monthly__exit_value", "construction__entry_price", "construction__exit_value"]:
        assert fields[name]["value"] == ""
    assert "checked" not in fields["monthly_enabled"]
    assert 'action="/compare/calculate#investment"' in response.text  # Existing calculator retained.


@pytest.mark.asyncio
async def test_json_all_four_strategies_have_separate_cash_asset_and_end_wealth(isolated_app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=isolated_app), base_url="http://test") as client:
        response = await client.post("/compare/strategies/calculate", json={
            "budget": 40_000_000, "monthly": monthly(), "daily": daily(), "construction": construction(),
            "phone": "PRIVATE-CONTACT", "description": "PRIVATE-RAW"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    result = body["result"]
    assert len(result["strategies"]) == 3 and len(result["banks"]) == 4
    assert result["strategies"][0]["end_wealth"] == 40_090_000
    assert result["strategies"][0]["net_operating_cash"] == 2_010_000
    assert result["strategies"][0]["asset_value_change"] == 1_000_000
    assert all(row["timeline"] is None for row in result["banks"])
    assert "PRIVATE" not in response.text
    assert body["html"].count('data-strategy-result="') == 7
    assert 'class="hs-chart-line' in body["html"]
    assert "К концу года проект ещё строится" in body["html"]
    assert "Стоимость объекта и проценты вклада по месяцам не распределяются" in body["html"]
    assert "2 010 000 ₸" in body["html"] and "40 090 000 ₸" in body["html"]
    assert "Лимит гарантии в этом банке" in body["html"]


@pytest.mark.asyncio
async def test_no_js_post_matches_json_and_preserves_selected_public_offer(isolated_app):
    values = monthly()
    data = {"budget": "40000000", "monthly_enabled": "on", "listing": "1000000001",
            "hex": "0:0", "rooms": "2", "area": "60", "scope": "city",
            **{"monthly__" + key: str(value) for key, value in values.items()}}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=isolated_app), base_url="http://test") as client:
        response = await client.post("/compare/strategies/calculate", data=data)
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "40 090 000 ₸" in response.text
    assert 'data-strategy-result="monthly"' in response.text
    assert 'data-strategy-result="daily"' not in response.text
    fields = Inputs(response.text).inputs
    assert fields["listing"]["value"] == "1000000001"
    assert fields["scope"]["value"] == "city"
    assert fields["monthly__exit_value"]["value"] == "31000000"
    assert "checked" in fields["monthly_enabled"]
    assert 'value="32000000"' in response.text  # Existing selected-offer price not overwritten by scenario.


@pytest.mark.asyncio
async def test_unchecked_strategy_ignores_its_blank_or_malicious_fields(isolated_app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=isolated_app), base_url="http://test") as client:
        response = await client.post("/compare/strategies/calculate", data={
            "budget": "40000000", "monthly__exit_value": '<script>alert("XSS")</script>'},
            headers={"accept": "application/json"})
    assert response.status_code == 200
    assert response.json()["result"]["strategies"] == []
    assert response.json()["html"].count('data-strategy-result="') == 4
    assert "XSS" not in response.text


@pytest.mark.asyncio
async def test_missing_fields_are_actionable_but_do_not_hide_ready_bank_rows(isolated_app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=isolated_app), base_url="http://test") as client:
        response = await client.post("/compare/strategies/calculate", json={"budget": 40_000_000, "monthly": {}})
    assert response.status_code == 200
    data = response.json()
    assert data["result"]["strategies"] == []
    assert len(data["result"]["banks"]) == 4
    assert "Цена покупки" in data["result"]["diagnostics"][0]["message"]
    assert 'data-strategy-diagnostic="monthly"' in data["html"]
    assert 'data-strategy-result="monthly"' not in data["html"]


@pytest.mark.asyncio
async def test_stale_or_short_bank_terms_do_not_get_placeholder_result_or_monthly_line(isolated_app, monkeypatch):
    catalog = deposit_catalog(datetime.now(timezone.utc) - timedelta(hours=49))
    monkeypatch.setattr(routes, "load_deposit_catalog", lambda: catalog)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=isolated_app), base_url="http://test") as client:
        response = await client.post("/compare/strategies/calculate", json={"budget": 40_000_000})
    data = response.json()
    assert data["result"]["banks"] == []
    assert data["html"].strip() == ""
    catalog = deposit_catalog(datetime.now(timezone.utc))
    for row in catalog["offers"]:
        row["term_months"] = 3
    monkeypatch.setattr(routes, "load_deposit_catalog", lambda: catalog)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=isolated_app), base_url="http://test") as client:
        response = await client.post("/compare/strategies/calculate", json={"budget": 40_000_000, "monthly": monthly()})
    assert response.json()["result"]["banks"] == []
    assert 'data-strategy-result="bank:' not in response.json()["html"]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [None, [], {"budget": True}, {"budget": float("nan")},
                                     {"budget": float("inf")}, {"budget": 1_000_000_001}])
async def test_bad_shared_budget_is_422_not_an_automatic_scenario(isolated_app, payload):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=isolated_app), base_url="http://test") as client:
        response = await client.post("/compare/strategies/calculate", content=json.dumps(payload),
                                     headers={"content-type": "application/json"})
    assert response.status_code == 422
    assert "error" in response.json()
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_bad_html_input_is_escaped_and_request_size_is_bounded(isolated_app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=isolated_app), base_url="http://test") as client:
        response = await client.post("/compare/strategies/calculate", data={"budget": '<script>EVIL</script>'})
        oversized = await client.post("/compare/strategies/calculate", content="X" * 16001)
    assert response.status_code == 400
    assert "Общий бюджет: требуется число" in response.text
    assert '<script>EVIL</script>' not in response.text
    assert '&lt;script&gt;EVIL&lt;/script&gt;' in response.text
    assert oversized.status_code == 413
    assert oversized.headers["cache-control"] == "no-store"


def test_chart_rejects_invalid_timeline_and_never_uses_bank_projection():
    assert routes._strategy_chart({"strategies": [], "banks": [{"timeline": [{"month": 1, "cumulative_net_cash": 100}]}]}) is None
    row = {"key": "monthly", "label": "Monthly", "timeline": [{"month": month, "cumulative_net_cash": month * 100} for month in range(1, 13)]}
    chart = routes._strategy_chart({"strategies": [row]})
    assert len(chart["series"][0]["points"].split()) == 13
    row["timeline"][5]["cumulative_net_cash"] = float("nan")
    assert routes._strategy_chart({"strategies": [row]}) is None


@pytest.mark.asyncio
async def test_malformed_json_zero_values_and_budget_reserve_errors(isolated_app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=isolated_app), base_url="http://test") as client:
        malformed = await client.post("/compare/strategies/calculate", content='{broken',
                                      headers={"content-type": "application/json"})
        zeroes = await client.post("/compare/strategies/calculate", data={
            "budget": "40000000", "monthly_enabled": "on",
            **{"monthly__" + key: str(value) for key, value in monthly(
                monthly_rent=0, vacancy_months=0, annual_owner_cost=0,
                wait_months=0, sell_cost_pct=0).items()}})
        reserve = await client.post("/compare/strategies/calculate", json={
            "budget": 32_300_000, "monthly": monthly(wait_months=12)})
    assert malformed.status_code == 422
    assert malformed.headers["cache-control"] == "no-store"
    assert zeroes.status_code == 200
    inputs = Inputs(zeroes.text).inputs
    for field in ("monthly_rent", "vacancy_months", "annual_owner_cost", "wait_months", "sell_cost_pct"):
        assert inputs["monthly__" + field]["value"] == "0"
    data = reserve.json()
    assert data["result"]["strategies"] == []
    assert "Не хватает 240 000 ₸" in data["result"]["diagnostics"][0]["message"]
    assert "32 540 000 ₸" in data["html"]
