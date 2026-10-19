"""Fresh one-market refreshes retain verified cards without inventing a peer.

All data is synthetic and PostgreSQL access is explicitly rejected.
"""
from datetime import datetime, timedelta, timezone
import copy

import httpx
import pytest

from bot.core.market_comparison import (
    build_market_snapshot, compare_market, load_market_snapshot, publish_market_snapshot,
)
from scripts import publish_market_comparison as publisher
from tests.test_daily_picks_public_page import snapshot as daily_snapshot
from tests.test_market_comparison import NOW, primary, secondary, snapshot


@pytest.fixture(autouse=True)
def isolated_database(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused:unused@127.0.0.1:1/hatuli_test_partial_market")

    async def reject(*args, **kwargs):
        raise AssertionError("Partial-market tests must not connect to PostgreSQL")

    monkeypatch.setattr("asyncpg.connect", reject)
    monkeypatch.setattr("asyncpg.create_pool", reject)


@pytest.mark.parametrize("available", ["primary", "secondary"])
def test_fresh_single_market_replaces_old_publication_without_inheriting_stale_offers(tmp_path, available):
    path = tmp_path / "market.json"
    publish_market_snapshot(snapshot(), path)
    previous = path.read_bytes()
    fresh_primary = [primary(index + 1) for index in range(5)]
    fresh_secondary = [secondary(index + 100000) for index in range(5)]
    if available == "primary":
        fresh_secondary = [secondary(999999, last_seen=NOW-timedelta(hours=73))]
    else:
        fresh_primary = [primary(999, last_seen_at=NOW-timedelta(hours=73))]
    current = build_market_snapshot(fresh_primary, fresh_secondary, now=NOW,
                                    coverage={"primary_stale_available": 11724})
    publish_market_snapshot(current, path)
    assert path.read_bytes() != previous
    safe = load_market_snapshot(path)
    missing = "secondary" if available == "primary" else "primary"
    assert safe["summary"][f"{available}_count"] == 5
    assert safe["summary"][f"{missing}_count"] == 0
    assert safe["summary"]["coverage"]["primary_stale_available"] == 11724
    assert {offer["market_type"] for offer in safe["offers"]} == {available}
    result = compare_market(safe, scope="city", now=NOW)
    assert result["stale"] is False
    assert result["stats"][missing]["median_per_m2"] is None
    assert result["delta_pct"] is None and result["comparable"] is False
    assert all(tier["delta_pct"] is None and not tier["comparable"] for tier in result["tiers"])
    assert not any("медиана за м² ниже" in text for stats in result["stats"].values()
                   for text in stats["advantages"])


@pytest.mark.parametrize("failure", ["both_empty", "invalid_url", "invalid_metadata"])
def test_empty_or_invalid_refresh_still_preserves_previous_snapshot(tmp_path, failure):
    path = tmp_path / "market.json"
    publish_market_snapshot(snapshot(), path)
    previous = path.read_bytes()
    data = snapshot(0, 0) if failure == "both_empty" else snapshot(0, 5)
    if failure == "invalid_url":
        data["offers"][0]["url"] = "https://example.org/private"
    if failure == "invalid_metadata":
        data["schema_version"] = -1
    with pytest.raises(ValueError):
        publish_market_snapshot(data, path)
    assert path.read_bytes() == previous


@pytest.mark.asyncio
async def test_partial_publisher_refresh_keeps_eleven_details_and_four_checked_reasons(tmp_path, monkeypatch):
    from bot.admin_web import create_admin_app
    from bot.public_comparison_routes import comparison_context
    from tests.test_public_comparison_routes import NoPrivateDB

    now = datetime.now(timezone.utc)
    daily = daily_snapshot(now, count=11)
    public_rows = []
    for item in daily["items"]:
        public_rows.append({"id": item["id"], "market_type": "secondary", "price": item["price"],
                            "rooms": item["rooms"], "area": item["area"], "address": item["address"],
                            "comparison_only": True, "public_comparison_listing": True})
    aggregate_rows = [secondary(index+100000, last_seen=now, rooms=2, area=50) for index in range(5)]

    async def read(dsn, observed_at):
        assert dsn == "postgresql://unused:unused@127.0.0.1:1/hatuli_test_partial_market"
        assert observed_at >= now
        return [], aggregate_rows+public_rows, 100, {"primary_available_recent": 0, "primary_stale_available": 11724}

    monkeypatch.setattr(publisher, "read_market_rows", read)
    path = tmp_path / "market.json"
    result = await publisher.run(output=path)
    assert result["published"] is True
    assert result["primary_count"] == 0 and result["secondary_count"] == 5
    market = load_market_snapshot(path)
    assert len(market["listing_details"]) == 11
    assert {row["id"] for row in market["listing_details"]} == {item["id"] for item in daily["items"]}
    monkeypatch.setattr("bot.public_comparison_routes.load_market_snapshot", lambda: market)
    monkeypatch.setattr("bot.public_comparison_routes.load_daily_picks", lambda: daily)
    monkeypatch.setattr("bot.public_comparison_routes.load_deposit_catalog", lambda: {"offers": []})
    monkeypatch.setattr("bot.public_comparison_routes.forecast_for_offer", lambda *args, **kwargs: {"status": "insufficient_history"})
    frozen = copy.deepcopy(daily)
    for item in daily["items"]:
        assert all(offer["id"] != item["id"] for offer in market["offers"])
        context = comparison_context(listing=item["id"], scope="city")
        selected = context["selected_offer"]
        assert selected["is_daily_pick"] is True
        assert selected["reasons"] == item["reasons"] and len(selected["reasons"]) == 4
        assert selected["selection_facts"] == item["selection_facts"]
        assert context["comparison"]["delta_pct"] is None
        assert context["comparison"]["rooms"] == item["rooms"]
        assert context["comparison"]["area"] == item["area"]
        assert context["comparison"]["stats"]["secondary"]["count"] == 5
    # A user-selected location/size keeps its explicit filter behavior.
    filtered = comparison_context(listing=daily["items"][0]["id"], hex_id="0:0", rooms=3, area=80)
    assert filtered["comparison"]["rooms"] == 3 and filtered["comparison"]["area"] == 80
    app = create_admin_app(NoPrivateDB(), "test-form-secret", "test")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/compare?listing={daily['items'][0]['id']}&scope=city")
    assert response.status_code == 200
    assert response.text.count("data-daily-picks-factor") == 4
    assert response.text.count('class="mc-market-card"') == 1
    assert "Базовая цена первички ниже" not in response.text
    assert "Базовая цена первички выше" not in response.text
    assert "Первичка дешевле на" not in response.text
    assert "Первичка дороже на" not in response.text
    assert daily == frozen
