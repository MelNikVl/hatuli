"""One-amount calculation never fills missing property outcomes or opens a DB."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from bot.core.automatic_investment import calculate_automatic_investment

NOW = datetime(2026, 10, 19, 13, tzinfo=timezone.utc)


def complexes(now=NOW):
    return {"schema_version": 1, "scope": "astana", "generated_at": now.isoformat(), "count": 4,
            "items": [{"id": 10, "name": "Дом А", "label": "Дом А", "is_umbrella": False,
                       "parent_id": None, "child_ids": []},
                      {"id": 20, "name": "Зонтик Б", "label": "Зонтик Б", "is_umbrella": True,
                       "parent_id": None, "child_ids": [21]},
                      {"id": 21, "name": "Дом Б1", "label": "Зонтик Б · Дом Б1", "is_umbrella": False,
                       "parent_id": 20, "child_ids": []},
                      {"id": 30, "name": "Без объявлений", "label": "Без объявлений", "is_umbrella": False,
                       "parent_id": None, "child_ids": []}]}


def deposits(now=NOW):
    return {"offers": [{"id": "freedom-12", "bank_key": "freedom", "name": "На год",
                        "status": "verified", "checked_at": now.isoformat(), "currency": "KZT",
                        "term_months": 12, "rate_type": "fixed", "effective_pct": 18,
                        "nominal_pct": 16.5, "min_amount": 1000, "max_amount": None,
                        "guarantee_limit": 10_000_000, "deposit_type": "other",
                        "can_withdraw": False, "can_top_up": False,
                        "source_url": "https://bankffin.kz/ru/deposits/physical", "conditions": [], "warnings": []}]}


def observation(now=NOW):
    return {"id": "1000000001", "source_url": "https://krisha.kz/a/show/1000000001",
            "price": 30_000_000, "rooms": 2, "area": 60, "floor": 8, "floors_total": 11,
            "address": "улица Сыганак, 12/1", "observed_at": now.isoformat(),
            "source_status": "active", "source_checked_at": now.isoformat(),
            "complex_identity": {"checked": True, "status": "catalog_bound", "canonical_id": 10,
                                 "canonical_name": "Дом А", "umbrella_id": None, "umbrella_name": None,
                                 "house_id": None, "house_label": None, "binding_method": "url"}}


def rent_estimate(now=NOW):
    return {"monthly_median": 300_000, "monthly_low": 280_000, "monthly_high": 320_000,
            "sample_count": 5, "observed_at": now.isoformat(), "oldest_observed_at": now.isoformat(),
            "newest_observed_at": now.isoformat(), "basis": "same_complex", "asking_prices": True}


def sources(now=NOW):
    item = observation(now)
    return {"catalog": deposits(now), "complexes": complexes(now), "now": now,
            "picks": {"selection_date": now.date().isoformat(), "generated_at": now.isoformat(), "items": [item]},
            "market": {"generated_at": now.isoformat(), "listing_details": [],
                       "offers": [{**item, "complex_id": 10, "market_type": "secondary",
                                   "rental_estimate": rent_estimate(now)}]}}


@pytest.fixture(autouse=True)
def no_database(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://placeholder:placeholder@127.0.0.1:1/placeholder")
    async def denied(*args, **kwargs):
        raise AssertionError("Automatic comparison must not open PostgreSQL")
    monkeypatch.setattr("asyncpg.connect", denied)
    monkeypatch.setattr("asyncpg.create_pool", denied)


def test_same_budget_banks_real_price_remainder_and_asking_rent_are_separate():
    result = calculate_automatic_investment({"budget": "40 000 000"}, **sources())
    assert result["budget"] == 40_000_000
    bank = result["banks"][0]
    assert bank["initial_outlay"] == 40_000_000
    assert bank["total_profit"] == 7_200_000
    assert bank["end_wealth"] == 47_200_000
    row = result["properties"][0]
    assert row["price"] == 30_000_000 and row["cash_remainder"] == 10_000_000
    assert row["annual_gross_ceiling"] == 3_600_000
    assert row["gross_yield_on_budget_pct"] == 9
    assert row["bank_threshold"]["annual_gross_difference"] == -3_600_000
    assert row["bank_threshold"]["minimum_monthly_net_cash"] == 600_001
    assert row["bank_threshold"]["minimum_monthly_net_cash"]*12 > result["best_bank"]["total_profit"]
    assert "net_operating_cash" not in row and "exit_value" not in row and "end_wealth" not in row
    assert result["strategies"] == [] and result["purchases"] == []


def test_payload_cannot_inject_user_costs_occupancy_prices_or_alternate_scope():
    regular = calculate_automatic_investment({"budget": 40_000_000}, **sources())
    malicious = calculate_automatic_investment({"budget": 40_000_000, "monthly": {"monthly_rent": 9_999_999},
                                               "daily": {"occupancy_pct": 100}, "construction": {"exit_value": 1e9},
                                               "horizon_months": 100, "rooms": 6, "scope": "hex", "price": 1}, **sources())
    assert malicious == regular


def test_all_city_not_current_hex_rooms_or_selected_sale_price():
    data = sources()
    data["market"]["offers"][0].update(hex_id="900:900")
    row = calculate_automatic_investment({"budget": 40_000_000, "rooms": 1, "hex": "0:0"}, **data)["properties"][0]
    assert row["rooms"] == 2 and row["price"] == 30_000_000


def test_optional_complex_uses_catalog_even_without_any_developer_units():
    data = sources()
    assert calculate_automatic_investment({"budget": 40_000_000, "complex_id": "10"}, **data)["properties"]
    result = calculate_automatic_investment({"budget": 40_000_000, "complex_id": 30}, **data)
    assert result["complex_id"] == 30 and result["location_label"] == "Без объявлений"
    assert result["properties"] == [] and len(result["banks"]) == 1


@pytest.mark.parametrize("cid", [999, 0, -1, True, "10.0", "<script>", "1" * 80, [], {}])
def test_unknown_or_malformed_complex_is_rejected(cid):
    with pytest.raises(ValueError, match="ЖК"):
        calculate_automatic_investment({"budget": 40_000_000, "complex_id": cid}, **sources())


def test_umbrella_expands_only_real_children_and_requires_physical_house():
    data = sources()
    for row in [data["picks"]["items"][0], data["market"]["offers"][0]]:
        row["complex_identity"].update(status="house_from_source", canonical_id=21, canonical_name="Дом Б1",
                                       house_id=21, house_label="Дом Б1", umbrella_id=20,
                                       umbrella_name="Зонтик Б", binding_method="address")
    data["market"]["offers"][0]["complex_id"] = 21
    result = calculate_automatic_investment({"budget": 40_000_000, "complex_id": 20}, **data)
    assert result["properties"][0]["complex_id"] == 21
    assert calculate_automatic_investment({"budget": 40_000_000, "complex_id": 10}, **data)["properties"] == []
    data["picks"]["items"][0]["complex_identity"] = observation()["complex_identity"]
    data["picks"]["items"][0]["complex_identity"].update(canonical_id=20, canonical_name="Зонтик Б")
    assert calculate_automatic_investment({"budget": 40_000_000, "complex_id": 20}, **data)["properties"] == []


@pytest.mark.parametrize("change", ["price", "rooms", "area", "floor", "floors_total", "complex_id"])
def test_later_changed_source_or_binding_cannot_keep_old_rental_result(change):
    data = sources()
    row = data["market"]["offers"][0]
    row[change] += 1
    result = calculate_automatic_investment({"budget": 40_000_000}, **data)
    assert result["properties"] == [] and result["purchases"] == []
    assert result["banks"]


def test_changed_supplemental_price_also_excludes_offer():
    data = sources()
    data["market"]["listing_details"] = [{"id": "1000000001", "current_price": 29_000_000}]
    result = calculate_automatic_investment({"budget": 40_000_000}, **data)
    assert not result["properties"] and not result["purchases"]


@pytest.mark.parametrize("change", ["sample_count", "oldest_observed_at", "basis", "asking_prices"])
def test_missing_or_unqualified_rents_show_only_known_purchase_facts(change):
    data = sources()
    bad = {"sample_count": 4, "oldest_observed_at": (NOW-timedelta(hours=73)).isoformat(),
           "basis": "city_median", "asking_prices": False}
    data["market"]["offers"][0]["rental_estimate"][change] = bad[change]
    result = calculate_automatic_investment({"budget": 40_000_000}, **data)
    assert result["properties"] == [] and len(result["purchases"]) == 1
    assert "rental_estimate" not in result["purchases"][0]
    assert "gross_yield_on_budget_pct" not in result["purchases"][0]


def test_market_rows_without_positive_daily_identity_never_become_recommendations():
    data = sources()
    data["picks"]["items"] = []
    assert calculate_automatic_investment({"budget": 40_000_000}, **data)["properties"] == []
    data = sources()
    data["picks"]["items"][0].pop("complex_identity")
    result = calculate_automatic_investment({"budget": 40_000_000}, **data)
    assert result["properties"] == [] and result["purchases"] == []


def test_source_address_only_and_stale_receipts_are_not_promoted():
    data = sources()
    data["picks"]["items"][0]["source_checked_at"] = (NOW-timedelta(hours=27)).isoformat()
    result = calculate_automatic_investment({"budget": 40_000_000}, **data)
    assert not result["properties"] and not result["purchases"]


def test_stale_catalog_or_market_hides_properties_without_hiding_bank_math():
    for key, hours in [("complexes", 169), ("market", 27)]:
        data = sources()
        data[key]["generated_at"] = (NOW-timedelta(hours=hours)).isoformat()
        result = calculate_automatic_investment({"budget": 40_000_000}, **data)
        assert not result["properties"] and not result["purchases"] and result["banks"]


def test_bank_term_freshness_and_capital_limits_are_preserved():
    for changes in [{"term_months": 3}, {"checked_at": (NOW-timedelta(hours=49)).isoformat()},
                    {"max_amount": 35_000_000}, {"status": "review_required"}]:
        data = sources()
        data["catalog"]["offers"][0].update(changes)
        result = calculate_automatic_investment({"budget": 40_000_000}, **data)
        assert result["banks"] == [] and result["best_bank"] is None
        assert result["properties"][0]["bank_threshold"] is None
        assert result["diagnostics"] == []


def test_no_unchecked_rollover_or_placeholder_when_every_source_is_missing():
    result = calculate_automatic_investment({"budget": 40_000_000}, catalog={"offers": []},
                                           market={}, picks=None, complexes=None, now=NOW)
    assert result["banks"] == [] and result["properties"] == [] and result["purchases"] == []


@pytest.mark.parametrize("budget", [None, True, "", "NaN", float("inf"), 0, -10, 1_000_000_001, [], {}])
def test_invalid_or_unbounded_budget_is_rejected(budget):
    with pytest.raises(ValueError, match="бюджет"):
        calculate_automatic_investment({"budget": budget}, **sources())


def test_budget_boundary_and_unicode_thousands_separators():
    result = calculate_automatic_investment({"budget": "40\u202f000\u00a0000"}, **sources())
    assert result["budget"] == 40_000_000
    data = sources()
    result = calculate_automatic_investment({"budget": 29_999_999}, **data)
    assert not result["properties"] and not result["purchases"]


def test_deduplication_bounded_output_and_input_immutability():
    data = sources()
    original = deepcopy(data)
    data["picks"]["items"].append(deepcopy(data["picks"]["items"][0]))
    before = deepcopy(data)
    result = calculate_automatic_investment({"budget": 40_000_000}, **data)
    assert len(result["properties"]) == 1 and data == before
    assert original["market"] == data["market"]

def test_nested_umbrella_follows_reciprocal_catalog_children_only():
    data = sources()
    data["complexes"]["items"][0].update(parent_id=21)
    inner = data["complexes"]["items"][2]
    inner.update(is_umbrella=True, child_ids=[10])
    for row in (data["picks"]["items"][0], data["market"]["offers"][0]):
        row["complex_identity"].update(status="house_from_source", house_id=10,
            house_label="Дом А", umbrella_id=21, umbrella_name="Дом Б1", binding_method="address")
    result = calculate_automatic_investment({"budget": 40_000_000, "complex_id": 20}, **data)
    assert [row["complex_id"] for row in result["properties"]] == [10]
    data["complexes"]["items"][0]["parent_id"] = None
    assert not calculate_automatic_investment({"budget": 40_000_000, "complex_id": 20}, **data)["properties"]


def test_displayed_whole_tenge_rent_threshold_really_exceeds_bank_income():
    data = sources()
    data["catalog"]["offers"][0]["effective_pct"] = 17.5
    result = calculate_automatic_investment({"budget": 40_000_000}, **data)
    threshold = result["properties"][0]["bank_threshold"]
    assert threshold["annual_income"] == 7_000_000
    assert threshold["minimum_monthly_net_cash"] == 583_334
    assert round(threshold["minimum_monthly_net_cash"])*12 > threshold["annual_income"]
