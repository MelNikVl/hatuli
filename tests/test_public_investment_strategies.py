"""Budget-only public flow: full optional catalogue, no request-time DB access."""
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import json

import httpx
import pytest

from bot import public_comparison_routes as routes
from bot.admin_web import create_admin_app
from bot.core.comparison_complexes import build_comparison_complexes
from tests.test_public_comparison_routes import NoPrivateDB, deposit_catalog, market_snapshot, daily_picks


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
        raise AssertionError("Budget calculator cannot open PostgreSQL")
    monkeypatch.setattr("asyncpg.connect", deny)
    monkeypatch.setattr("asyncpg.create_pool", deny)
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(routes, "load_market_snapshot", lambda: market_snapshot(now))
    monkeypatch.setattr(routes, "load_deposit_catalog", lambda: deposit_catalog(now))
    monkeypatch.setattr(routes, "load_daily_picks", lambda: daily_picks(now))
    monkeypatch.setattr(routes, "forecast_for_offer", lambda *a, **k: None)
    catalog = build_comparison_complexes([
        {"id": 10, "name": "Дом А", "lat": 51.12, "lon": 71.42},
        {"id": 30, "name": "ЖК без текущих продаж", "lat": 51.13, "lon": 71.43},
    ], now=now)
    monkeypatch.setattr(routes, "load_comparison_complexes", lambda: catalog)
    return create_admin_app(NoPrivateDB(), "isolated-budget-form", "test")


@pytest.mark.asyncio
async def test_only_budget_is_required_and_full_catalog_is_optional(isolated_app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=isolated_app), base_url="http://test") as client:
        page = await client.get("/compare")
        chosen = await client.get("/compare?listing=1000000001")
    assert page.status_code == chosen.status_code == 200
    fields = Inputs(page.text).inputs
    assert fields["budget"]["value"] == ""
    assert {name for name, attrs in fields.items() if "required" in attrs} == {"budget"}
    assert Inputs(chosen.text).inputs["budget"]["value"] == "32000000"
    assert 'name="complex_id"' in page.text and "ЖК без текущих продаж" in page.text
    assert 'data-mc-calculator' not in page.text and 'name="repair_cost"' not in page.text
    assert 'name="annual_growth_pct"' not in page.text
    assert 'class="mc-section hs-section ym-hide-content"' in page.text


@pytest.mark.asyncio
async def test_budget_alone_calculates_all_eligible_banks(isolated_app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=isolated_app), base_url="http://test") as client:
        response = await client.post("/compare/strategies/calculate", json={"budget": 40_000_000})
    data = response.json()
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert data["result"]["mode"] == "automatic"
    assert len(data["result"]["banks"]) == 4
    assert data["html"].count('data-strategy-result="bank:') == 4
    assert data["html"] == data["results_html"]
    assert "Заполните" not in data["html"]


@pytest.mark.asyncio
async def test_manual_fields_cannot_forge_rent_occupancy_or_resale(isolated_app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=isolated_app), base_url="http://test") as client:
        first = await client.post("/compare/strategies/calculate", json={"budget": 40_000_000})
        attack = await client.post("/compare/strategies/calculate", json={
            "budget": 40_000_000, "monthly": {"monthly_rent": 999_999_999, "exit_value": 1e9},
            "daily": {"occupancy_pct": 100}, "construction": {"exit_value": 1e9},
            "seller_phone": "PRIVATE-CONTACT"})
    assert first.json() == attack.json()
    assert "PRIVATE-CONTACT" not in attack.text


@pytest.mark.asyncio
async def test_native_html_post_and_catalog_without_developer_units_work(isolated_app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=isolated_app), base_url="http://test") as client:
        response = await client.post("/compare/strategies/calculate", data={"budget": "40000000", "complex_id": "30"})
        get_page = await client.get("/compare?budget=40000000&complex_id=30")
    for page in (response, get_page):
        assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
        assert page.text.count('data-strategy-result="bank:') == 4
        assert 'value="30"' in page.text and "ЖК без текущих продаж" in page.text
        assert Inputs(page.text).inputs["budget"]["value"] == "40000000"


@pytest.mark.asyncio
async def test_unknown_complex_is_rejected_without_silently_using_city(isolated_app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=isolated_app), base_url="http://test") as client:
        response = await client.post("/compare/strategies/calculate", json={"budget": 40_000_000, "complex_id": 999})
    assert response.status_code == 422 and "ЖК" in response.json()["error"]
    assert "banks" not in response.json()


@pytest.mark.asyncio
async def test_stale_banks_do_not_receive_fake_interest(isolated_app, monkeypatch):
    catalog = deposit_catalog(datetime.now(timezone.utc)-timedelta(hours=49))
    monkeypatch.setattr(routes, "load_deposit_catalog", lambda: catalog)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=isolated_app), base_url="http://test") as client:
        response = await client.post("/compare/strategies/calculate", json={"budget": 40_000_000})
    data = response.json()
    assert data["result"]["banks"] == []
    assert 'data-strategy-result="bank:' not in data["html"]
    assert "data-strategy-empty" in data["html"]  # A specific filter response, not a silent blank.


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [None, [], {"budget": True}, {"budget": float("nan")},
                                     {"budget": float("inf")}, {"budget": 1_000_000_001}])
async def test_invalid_budget_has_no_result_or_stale_html(isolated_app, payload):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=isolated_app), base_url="http://test") as client:
        response = await client.post("/compare/strategies/calculate", content=json.dumps(payload),
                                     headers={"content-type": "application/json"})
    assert response.status_code == 422 and "error" in response.json()
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_malformed_json_xss_and_large_body_are_bounded(isolated_app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=isolated_app), base_url="http://test") as client:
        malformed = await client.post("/compare/strategies/calculate", content="{broken",
                                      headers={"content-type": "application/json"})
        escaped = await client.post("/compare/strategies/calculate", data={"budget": "<script>EVIL</script>"})
        oversized = await client.post("/compare/strategies/calculate", content="X"*16001)
    assert malformed.status_code == 422 and oversized.status_code == 413
    assert escaped.status_code == 400
    assert '<script>EVIL</script>' not in escaped.text
    assert '&lt;script&gt;EVIL&lt;/script&gt;' in escaped.text
    assert "data-strategy-ready" not in escaped.text
