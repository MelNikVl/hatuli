"""Confirmed risk facts stay visible without restoring generic warnings."""
from copy import deepcopy

import pytest

from bot.core import daily_picks as picks, daily_picks_display as display
from scripts import publish_daily_picks as publisher
from tests.test_daily_picks import NOW, confirmed, rows


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    async def reject(*args, **kwargs):
        raise AssertionError("No database access in source fact tests")
    monkeypatch.setattr("asyncpg.connect", reject)
    monkeypatch.setattr("asyncpg.create_pool", reject)
    monkeypatch.setattr(display, "load_market_snapshot", lambda: {"offers": [], "listing_details": []})
    monkeypatch.setattr(display, "forecast_for_offer", lambda *args, **kwargs: None)


def snapshot():
    return confirmed(picks.build_daily_picks(rows(), NOW))


def record(item, **changes):
    return {key: item[key] for key in ("id", "source_url", "price", "rooms", "area", "floor", "floors_total")} | {
        "status": "active", "sale_astana": True, "checked_at": NOW.isoformat(), **changes}


def test_matching_browser_pledge_observation_survives_public_projection():
    data = snapshot()
    checks = {item["id"]: record(item, is_pledged=True) for item in data["items"]}
    checked, _, _ = publisher.apply_source_checks(data, checks)
    safe = picks.validate_snapshot(checked)
    card = display.prepare_daily_picks(safe, NOW)["items"][0]
    assert card["source_facts"] == {"is_pledged": True}
    assert card["evidence_notices"] == ["В залоге — указано в исходном объявлении."]
    assert len(card["reasons"]) == 3


@pytest.mark.parametrize("value", [False, None])
def test_absence_of_pledge_label_is_not_a_clean_title_claim(value):
    data = snapshot()
    card = data["items"][0]
    card["source_facts"] = {} if value is None else {"is_pledged": value}
    safe = picks.validate_snapshot(data)
    assert safe["items"][0]["source_facts"] == {}
    assert display.prepare_daily_picks(safe, NOW)["items"][0]["evidence_notices"] == []


@pytest.mark.parametrize("facts", [{"is_pledged": "true"}, {"seller_phone": "+7 placeholder"}, [True]])
def test_public_projection_rejects_unknown_or_untyped_source_fields(facts):
    data = deepcopy(snapshot())
    data["items"][0]["source_facts"] = facts
    with pytest.raises(ValueError, match="confirmed source facts"):
        picks.validate_snapshot(data)


def test_untyped_browser_observation_cannot_become_a_public_fact():
    data = snapshot()
    item = data["items"][0]
    with pytest.raises(ValueError, match="pledge observation"):
        publisher.apply_source_checks(data, {item["id"]: record(item, is_pledged="true")})
