from datetime import datetime, timedelta, timezone
import json
from copy import deepcopy

import pytest

from bot.core.deposit_comparison import compare_investment, public_deposit_catalog, load_deposit_catalog

NOW = datetime(2026, 10, 13, 8, tzinfo=timezone.utc)


def offer(**kwargs):
    base = {"id": "freedom-capital-12", "bank_key": "freedom", "name": "Капитал",
            "currency": "KZT", "term_months": 12, "nominal_pct": 15.7, "effective_pct": 16.87,
            "min_amount": 100_000, "max_amount": None, "guarantee_limit": 20_000_000,
            "can_withdraw": False, "can_top_up": False, "rate_type": "fixed",
            "deposit_type": "savings",
            "status": "verified", "checked_at": NOW.isoformat(),
            "source_url": "https://bankffin.kz/ru/deposits/physical/capital", "conditions": [], "warnings": []}
    return {**base, **kwargs}


def catalog(*offers):
    return {"schema_version": 1, "checked_at": NOW.isoformat(), "offers": list(offers), "sources": []}


def scenario(**kwargs):
    return {"price": 30_000_000, "repair_cost": 3_000_000, "buy_cost": 300_000,
            "monthly_rent": 200_000, "annual_owner_cost": 120_000, "vacancy_months": 0,
            "wait_months": 0, "annual_growth_pct": 0, "mode": "live", **kwargs}


def test_same_initial_capital_apy_once_and_no_double_avoided_rent():
    result = compare_investment(scenario(), catalog(offer()), NOW)
    bank = result["banks"][0]
    assert result["initial_capital"] == 33_300_000
    assert bank["deposit_income"] == 5_617_710
    assert bank["deposit_end_value"] == 38_917_710
    assert bank["deposit_cash_after_rent"] == 36_517_710
    assert result["property"]["wealth"] == 29_880_000
    assert bank["property_advantage"] == -6_637_710
    assert bank["break_even_growth_pct"] == 22.1257
    assert bank["future_purchase_surplus"] == 3_217_710
    assert bank["guaranteed_amount"] == 20_000_000
    assert bank["unguaranteed_amount"] == 18_917_710


def test_rental_income_is_cash_flow_and_never_saved_personal_rent():
    result = compare_investment(scenario(mode="rent_out", wait_months=3, vacancy_months=1), catalog(offer()), NOW)
    assert result["property"]["rent_months"] == 8
    assert result["property"]["gross_rent"] == 1_600_000
    assert result["property"]["wealth"] == 31_480_000
    assert result["property"]["avoided_rent"] == 0
    assert result["banks"][0]["wait_rent_cost"] == 0


@pytest.mark.parametrize("mode", ["live", "rent_out"])
def test_unfinished_primary_has_no_first_year_income_or_saved_rent(mode):
    result = compare_investment(scenario(mode=mode, wait_months=18), catalog(offer()), NOW)
    assert result["property"]["gross_rent"] == 0
    assert result["property"]["avoided_rent"] == 0
    assert result["property"]["wealth"] == (27_480_000 if mode == "live" else 29_880_000)


def test_sale_costs_and_growth_scenario_change_value_not_deposit_return():
    result = compare_investment(scenario(annual_growth_pct=10, sell_cost_pct=2), catalog(offer()), NOW)
    assert result["future_apartment_price"] == 33_000_000
    assert result["property"]["sale_cost"] == 660_000
    assert result["property"]["wealth"] == 32_220_000
    assert result["banks"][0]["deposit_income"] == 5_617_710
    assert result["banks"][0]["future_purchase_cost"] == 36_300_000


def test_break_even_is_actually_equal_wealth_with_expenses_and_sale_cost():
    original = compare_investment(scenario(sell_cost_pct=2, wait_months=5), catalog(offer()), NOW)
    growth = original["banks"][0]["break_even_growth_pct"]
    result = compare_investment(scenario(sell_cost_pct=2, wait_months=5, annual_growth_pct=growth), catalog(offer()), NOW)
    assert abs(result["banks"][0]["property_advantage"]) < 20  # threshold rounded to 4 decimals


def test_shortfall_when_future_price_growth_exceeds_deposit_savings():
    result = compare_investment(scenario(annual_growth_pct=50), catalog(offer()), NOW)
    assert result["banks"][0]["future_purchase_surplus"] < 0


@pytest.mark.parametrize("field,value", [
    ("price", True), ("price", float("nan")), ("price", float("inf")), ("price", -1),
    ("repair_cost", -1), ("monthly_rent", -1), ("annual_owner_cost", -1),
    ("vacancy_months", 13), ("wait_months", 100), ("sell_cost_pct", 31),
    ("annual_growth_pct", -100), ("mode", "mortgage"),
])
def test_invalid_user_inputs_rejected(field, value):
    with pytest.raises(ValueError):
        compare_investment(scenario(**{field: value}), catalog(offer()), NOW)


@pytest.mark.parametrize("changes", [
    {"term_months": 13}, {"currency": "USD"}, {"rate_type": "floating"}, {"rate_type": None},
    {"effective_pct": float("nan")}, {"effective_pct": True}, {"effective_pct": "20"},
    {"can_top_up": None}, {"can_withdraw": "yes"}, {"max_amount": 30_000_000},
    {"min_amount": 100_000_000}, {"status": "needs_review"},
    {"checked_at": (NOW - timedelta(hours=49)).isoformat()},
    {"checked_at": "2026-10-13T08:00:00"}, {"checked_at": (NOW + timedelta(days=1)).isoformat()},
    {"source_url": "javascript:alert(1)"}, {"source_url": "https://bankffin.kz.evil.test/foo"},
    {"source_url": "https://user@bankffin.kz/foo"}, {"source_url": "https://bankffin.kz:443/foo"},
    {"guarantee_limit": 30_000_000},
    {"effective_pct": 12}, {"deposit_type": "other"}, {"deposit_type": None},
])
def test_uncertain_program_never_calculated(changes):
    bad = offer(**changes)
    result = compare_investment(scenario(), catalog(bad), NOW)
    assert not result["banks"][0]["eligible"]
    assert "deposit_income" not in result["banks"][0]
    assert result["banks"][0]["source_url"].startswith("https://bankffin.kz/")


def test_only_new_fixed_twelve_month_program_can_win_rate_sort():
    items = [offer(), offer(id="13-month", term_months=13, effective_pct=25),
             offer(id="expired", checked_at=(NOW - timedelta(days=3)).isoformat(), effective_pct=29)]
    result = compare_investment(scenario(), catalog(*items), NOW)
    assert result["banks"][0]["effective_pct"] == 16.87


def test_four_public_bank_rows_and_safe_missing_catalog():
    result = public_deposit_catalog(catalog(None, {}, offer(source_url="javascript:alert(1)")), NOW)
    assert [bank["key"] for bank in result["banks"]] == ["freedom", "forte", "bereke", "home_credit"]
    assert result["stale"] is True
    assert all(bank["source_url"].startswith("https://") for bank in result["banks"])
    assert not any(bank["eligible"] for bank in result["banks"])


def test_catalog_loader_fails_closed_for_missing_invalid_schema_and_json(tmp_path):
    path = tmp_path / "catalog.json"
    assert load_deposit_catalog(path)["offers"] == []
    path.write_text("broken", encoding="utf8")
    assert load_deposit_catalog(path)["offers"] == []
    path.write_text(json.dumps({"schema_version": 99, "offers": []}), encoding="utf8")
    assert load_deposit_catalog(path)["offers"] == []


def test_catalog_is_not_mutated_by_calculation():
    data = catalog(offer())
    original = deepcopy(data)
    compare_investment(scenario(), data, NOW)
    assert data == original
