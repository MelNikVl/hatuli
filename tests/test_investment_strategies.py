"""Pure scenarios with synthetic official offers; never open PostgreSQL."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import pytest

from bot.core.deposit_comparison import compare_investment
from bot.core.investment_strategies import calculate_strategy_comparison

NOW = datetime(2026, 10, 19, 10, tzinfo=timezone.utc)


def offer(**changes):
    return {"id": "synthetic-fixed12", "bank_key": "freedom", "name": "Синтетический вклад",
            "currency": "KZT", "term_months": 12, "nominal_pct": 15, "effective_pct": 16,
            "min_amount": 100_000, "max_amount": None, "guarantee_limit": 20_000_000,
            "can_withdraw": False, "can_top_up": False, "rate_type": "fixed",
            "deposit_type": "savings", "status": "verified", "checked_at": NOW.isoformat(),
            "source_url": "https://bankffin.kz/ru/deposits/physical/capital",
            "conditions": [], "warnings": [], **changes}


def catalog(*offers):
    return {"schema_version": 1, "checked_at": NOW.isoformat(), "offers": list(offers), "sources": []}


def monthly(**changes):
    return {"purchase_price": 30_000_000, "repair_cost": 2_000_000, "buy_cost": 300_000,
            "monthly_rent": 250_000, "annual_owner_cost": 240_000, "vacancy_months": 1,
            "wait_months": 2, "exit_value": 31_000_000, "sell_cost_pct": 2, **changes}


def daily(**changes):
    return {"purchase_price": 30_000_000, "repair_cost": 3_000_000, "buy_cost": 300_000,
            "adr": 20_000, "occupancy_pct": 60, "platform_fee_pct": 10, "management_fee_pct": 15,
            "cleaning_cost_per_stay": 4_000, "stays_per_month": 6, "annual_owner_cost": 360_000,
            "wait_months": 0, "exit_value": 30_000_000, "sell_cost_pct": 2, **changes}


def construction(**changes):
    return {"entry_price": 25_000_000, "finishing_cost": 4_000_000, "buy_cost": 500_000,
            "wait_months": 15, "exit_value": 29_000_000, "sell_cost_pct": 2,
            "annual_owner_cost": 60_000, **changes}


def calculate(**changes):
    payload = {"budget": 40_000_000, "horizon_months": 12, **changes}
    return calculate_strategy_comparison(payload, catalog(offer()), NOW)


def test_identical_budget_reconciles_cash_upfront_costs_asset_change_and_final_wealth():
    result = calculate(monthly=monthly(), daily=daily(), construction=construction())
    assert result["budget"] == 40_000_000
    assert result["horizon_months"] == 12
    assert len(result["strategies"]) == 3
    for row in result["strategies"]:
        assert row["initial_outlay"] + row["cash_remainder"] == result["budget"]
        assert row["end_wealth"] == pytest.approx(row["cash_remainder"] + row["exit_value"] - row["exit_cost"] + row["net_operating_cash"])
        assert row["total_profit"] == pytest.approx(row["net_operating_cash"] + row["asset_value_change"] - row["upfront_expenses"] - row["exit_cost"])
        assert row["total_return_pct"] == pytest.approx(row["total_profit"] / 40_000_000 * 100, abs=0.00005)
        assert row["evidence_type"] == "user_scenario"


def test_monthly_deducts_wait_vacancy_annual_expenses_and_sale_fees_exactly_once():
    row = calculate(monthly=monthly())["strategies"][0]
    assert row["gross_income"] == 250_000 * 9
    assert row["operating_expenses"] == 240_000
    assert row["net_operating_cash"] == 2_010_000
    assert row["upfront_expenses"] == 2_300_000
    assert row["exit_cost"] == 620_000
    assert row["cash_remainder"] == 7_700_000
    assert row["end_wealth"] == 40_090_000
    assert row["total_profit"] == 90_000
    assert row["timeline"][0]["net_cash"] == -20_000
    assert row["timeline"][1]["net_cash"] == -20_000
    assert row["timeline"][2]["gross_income"] == 225_000
    assert row["timeline"][-1]["cumulative_net_cash"] == row["net_operating_cash"]


def test_expensive_repair_does_not_manufacture_asset_appreciation():
    plain = calculate(monthly=monthly(repair_cost=0))["strategies"][0]
    renovated = calculate(monthly=monthly(repair_cost=5_000_000))["strategies"][0]
    assert plain["exit_value"] == renovated["exit_value"]
    assert plain["net_operating_cash"] == renovated["net_operating_cash"]
    assert plain["end_wealth"] - renovated["end_wealth"] == 5_000_000


def test_remainder_never_earns_implicit_bank_interest():
    small = calculate_strategy_comparison({"budget": 33_000_000, "monthly": monthly()}, catalog(offer()), NOW)
    large = calculate(monthly=monthly())
    assert small["strategies"][0]["total_profit"] == large["strategies"][0]["total_profit"]
    assert large["strategies"][0]["end_wealth"] - small["strategies"][0]["end_wealth"] == 7_000_000


def test_daily_uses_occupied_nights_both_fees_and_owner_cleaning_without_double_counting():
    row = calculate(daily=daily())["strategies"][0]
    assert row["gross_income"] == 20_000 * 365 * 0.6
    assert row["operating_expenses"] == 4_380_000 * 0.25 + 4_000 * 6 * 12 + 360_000
    assert row["net_operating_cash"] == 2_637_000
    assert row["total_profit"] == -1_263_000
    assert row["net_cash_yield_pct"] > 0
    assert row["total_return_pct"] < 0


def test_daily_wait_fraction_reduces_nights_and_stays_but_not_explicit_annual_cost():
    row = calculate(daily=daily(wait_months=1.5))["strategies"][0]
    assert row["gross_income"] == 4_380_000 * 10.5 / 12
    assert row["operating_expenses"] == 4_380_000 * 10.5 / 12 * 0.25 + 4_000 * 6 * 10.5 + 360_000
    assert row["timeline"][0]["gross_income"] == 0
    assert row["timeline"][1]["gross_income"] == 182_500


@pytest.mark.parametrize("key,section", [
    ("monthly", monthly(monthly_rent=0, vacancy_months=12)),
    ("monthly", monthly(wait_months=12)),
    ("daily", daily(occupancy_pct=0, stays_per_month=0)),
    ("daily", daily(wait_months=60)),
])
def test_no_earning_time_or_zero_rent_preserves_negative_holding_cash(key, section):
    row = calculate(**{key: section})["strategies"][0]
    assert row["gross_income"] == 0
    assert row["net_operating_cash"] == -section["annual_owner_cost"]


def test_construction_after_horizon_is_explicit_right_scenario_not_ready_sale_or_rent():
    row = calculate(construction=construction())["strategies"][0]
    assert row["asset_stage"] == "unfinished_right_scenario"
    assert row["gross_income"] == 0
    assert row["net_operating_cash"] == -60_000
    assert row["cash_remainder"] == 10_500_000
    assert row["end_wealth"] == 38_860_000
    assert row["delay_sensitivity"] == [
        {"extra_months": delay, "ready_by_horizon": False, "rental_income": 0, "exit_value_reestimated": False}
        for delay in (0, 3, 6)]
    assert any("переуступки" in text for text in row["assumptions"])


def test_construction_delay_changes_readiness_only_not_invented_price_or_rent():
    row = calculate(construction=construction(wait_months=9))["strategies"][0]
    assert row["asset_stage"] == "ready_scenario"
    assert [item["ready_by_horizon"] for item in row["delay_sensitivity"]] == [True, True, False]
    assert all(item["rental_income"] == 0 for item in row["delay_sensitivity"])


def test_affordability_is_per_strategy_and_other_ready_rows_survive():
    result = calculate(monthly=monthly(), daily=daily(purchase_price=50_000_000), construction=construction())
    assert [row["key"] for row in result["strategies"]] == ["monthly", "construction"]
    diagnostic = next(item for item in result["diagnostics"] if item["key"] == "daily")
    assert diagnostic["status"] == "unaffordable"
    assert diagnostic["required_budget"] == 53_300_000
    assert diagnostic["shortfall"] == 13_300_000
    assert len(result["banks"]) == 1


def test_absent_sections_not_enabled_or_filled_with_market_numbers():
    result = calculate()
    assert result["strategies"] == []
    assert not any(item["key"] in ("monthly", "daily", "construction") for item in result["diagnostics"])


@pytest.mark.parametrize("missing", list(monthly()))
def test_every_monthly_input_is_explicit_required_not_silently_zero(missing):
    section = monthly()
    section.pop(missing)
    result = calculate(monthly=section, daily=daily())
    assert [row["key"] for row in result["strategies"]] == ["daily"]
    diagnostic = next(item for item in result["diagnostics"] if item["key"] == "monthly")
    assert diagnostic["status"] == "incomplete"
    assert diagnostic["fields"] == [missing]


@pytest.mark.parametrize("value", [True, False, float("inf"), float("nan"), "NaN", "1e999", {}, [], -1])
def test_nonfinite_boolean_and_negative_money_are_not_scenarios(value):
    result = calculate(monthly=monthly(monthly_rent=value), construction=construction())
    assert [row["key"] for row in result["strategies"]] == ["construction"]
    diagnostic = next(item for item in result["diagnostics"] if item["key"] == "monthly")
    assert diagnostic["status"] == "invalid"
    assert diagnostic["fields"] == ["monthly_rent"]


@pytest.mark.parametrize("payload", [None, [], {}, {"budget": True}, {"budget": 0},
                                      {"budget": float("inf")}, {"budget": 1e20},
                                      {"budget": 1e6, "horizon_months": 6},
                                      {"budget": 1e6, "horizon_months": True}])
def test_bad_common_budget_or_horizon_fails_request(payload):
    with pytest.raises(ValueError):
        calculate_strategy_comparison(payload, catalog(offer()), NOW)


def test_numeric_form_strings_are_supported_without_mutating_user_input_or_catalog():
    payload = {"budget": "40000000", "horizon_months": "12", "monthly": {key: str(value) for key, value in monthly().items()}}
    data = catalog(offer())
    before = deepcopy((payload, data))
    assert calculate_strategy_comparison(payload, data, NOW)["strategies"][0]["total_profit"] == 90_000
    assert (payload, data) == before


@pytest.mark.parametrize("changes", [
    {"term_months": 3, "effective_pct": 25}, {"term_months": 13}, {"currency": "USD"},
    {"status": "needs_review"}, {"rate_type": "floating"}, {"can_withdraw": None},
    {"checked_at": (NOW - timedelta(hours=49)).isoformat()},
    {"source_url": "https://bankffin.kz.evil.test/offer"}, {"max_amount": 20_000_000},
    {"effective_pct": float("nan")}, {"guarantee_limit": 30_000_000},
])
def test_existing_bank_gate_excludes_uncertain_and_short_term_products(changes):
    result = calculate_strategy_comparison({"budget": 40_000_000, "monthly": monthly()}, catalog(offer(**changes)), NOW)
    assert result["banks"] == []
    assert result["best_bank_key"] is None
    assert result["strategies"][0]["bank_comparison"] is None
    json.dumps(result, allow_nan=False)


def test_bank_money_and_guarantee_match_existing_engine_and_no_fake_monthly_payouts():
    data = catalog(offer())
    result = calculate_strategy_comparison({"budget": 40_000_000}, data, NOW)
    legacy = compare_investment({"price": 40_000_000, "mode": "rent_out"}, data, NOW)["banks"][0]
    bank = result["banks"][0]
    assert bank["total_profit"] == legacy["deposit_income"] == 6_400_000
    assert bank["end_wealth"] == legacy["deposit_end_value"] == 46_400_000
    assert bank["guaranteed_amount"] == legacy["guaranteed_amount"] == 20_000_000
    assert bank["unguaranteed_amount"] == legacy["unguaranteed_amount"] == 26_400_000
    assert bank["timeline"] is None
    assert bank["evidence_type"] == "verified_bank_terms"


def test_best_bank_is_eligible_for_same_budget_and_full_year_not_higher_short_apy():
    data = catalog(offer(), offer(id="short", term_months=3, nominal_pct=20, effective_pct=25),
                   offer(id="forte", bank_key="forte", nominal_pct=16, effective_pct=17,
                         source_url="https://bank.forte.kz/ru/deposits"))
    result = calculate_strategy_comparison({"budget": 40_000_000, "daily": daily()}, data, NOW)
    assert result["best_bank_key"] == "bank:forte"
    assert result["strategies"][0]["bank_comparison"]["effective_pct"] == 17
    assert len(result["banks"]) == 2


def test_daily_threshold_reconciles_total_profit_with_bank_and_uses_exit_scenario():
    original = calculate(daily=daily())["strategies"][0]
    threshold = original["break_even_occupancy_pct"]
    assert threshold > 100  # This scenario cannot beat the bank even fully occupied.
    high_value = calculate(daily=daily(exit_value=37_000_000))["strategies"][0]
    assert high_value["break_even_occupancy_pct"] < threshold
    new = calculate(daily=daily(exit_value=37_000_000, occupancy_pct=high_value["break_even_occupancy_pct"]))
    assert abs(new["strategies"][0]["total_profit"] - new["banks"][0]["total_profit"]) < 10
    assert high_value["break_even_cash_occupancy_pct"] == original["break_even_cash_occupancy_pct"]


@pytest.mark.parametrize("changes", [{"adr": 0}, {"wait_months": 12},
                                    {"platform_fee_pct": 60, "management_fee_pct": 40},
                                    {"adr": 1e-320}])
def test_zero_or_tiny_threshold_denominator_never_returns_nan_or_infinity(changes):
    result = calculate(daily=daily(**changes))
    row = result["strategies"][0]
    assert row["break_even_occupancy_pct"] is None
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("changes", [{"occupancy_pct": 0}, {"stays_per_month": 31},
                                    {"platform_fee_pct": 60, "management_fee_pct": 50}])
def test_inconsistent_daily_stays_or_commissions_are_rejected(changes):
    result = calculate(daily=daily(**changes))
    assert result["strategies"] == []
    assert next(item for item in result["diagnostics"] if item["key"] == "daily")["status"] == "invalid"


def test_explicit_zero_exit_and_cash_loss_are_preserved_without_flooring_return():
    row = calculate(monthly=monthly(exit_value=0, monthly_rent=0))["strategies"][0]
    assert row["exit_value"] == 0
    assert row["asset_value_change"] == -30_000_000
    assert row["total_profit"] == -32_540_000
    assert row["total_return_pct"] < -80


def test_naive_clock_rejected_instead_of_bypassing_deposit_age_gate():
    with pytest.raises(ValueError, match="часовым поясом"):
        calculate_strategy_comparison({"budget": 40_000_000}, catalog(offer()), NOW.replace(tzinfo=None))


def test_daily_threshold_matches_monthly_operating_cash_and_bank_impossibility_is_explicit():
    result = calculate(monthly=monthly(), daily=daily())
    monthly_row, daily_row = result["strategies"]
    threshold = next(item for item in daily_row["thresholds"] if item["key"] == "daily_vs_monthly_cash")
    assert threshold["status"] == "attainable"
    equal = calculate(daily=daily(occupancy_pct=threshold["value"]))["strategies"][0]
    assert abs(equal["net_operating_cash"] - monthly_row["net_operating_cash"]) < 10
    impossible = next(item for item in daily_row["thresholds"] if item["key"] == "daily_vs_bank_total")
    assert impossible["value"] > 100
    assert impossible["status"] == "unattainable"


def test_construction_exit_threshold_matches_bank_with_remainder_and_all_costs():
    result = calculate(construction=construction())
    row = result["strategies"][0]
    threshold = row["minimum_exit_value_to_match_bank"]
    assert threshold == pytest.approx((29_500_000 + 6_400_000 + 60_000) / 0.98, abs=0.01)
    equal = calculate(construction=construction(exit_value=threshold))
    assert abs(equal["strategies"][0]["end_wealth"] - equal["banks"][0]["end_wealth"]) < 0.02
    assert row["thresholds"][0]["metric"] == "exit_value"
    assert row["asset_stage"] == "unfinished_right_scenario"


def test_construction_full_exit_fee_has_no_solvable_value_threshold():
    row = calculate(construction=construction(sell_cost_pct=100))["strategies"][0]
    assert row["minimum_exit_value_to_match_bank"] is None
    assert row["thresholds"][0]["status"] == "not_calculable"


def test_interim_cash_reserve_shortage_excludes_row_instead_of_borrowing_extra_money():
    result = calculate_strategy_comparison({"budget": 32_300_000, "monthly": monthly()}, catalog(offer()), NOW)
    assert result["strategies"] == []
    diagnostic = next(item for item in result["diagnostics"] if item["key"] == "monthly")
    assert diagnostic["status"] == "insufficient_reserve"
    assert diagnostic["required_budget"] == 32_340_000
    assert diagnostic["shortfall"] == 40_000
    assert len(result["banks"]) == 1
    funded = calculate_strategy_comparison({"budget": diagnostic["required_budget"], "monthly": monthly()}, catalog(offer()), NOW)
    assert funded["strategies"][0]["minimum_cash_balance"] == 0
    assert funded["strategies"][0]["total_profit"] == 90_000


def test_reserve_excluded_monthly_is_not_used_as_daily_threshold_benchmark():
    result = calculate_strategy_comparison({"budget": 32_300_000, "monthly": monthly(),
                                            "daily": daily(repair_cost=0)}, catalog(offer()), NOW)
    assert [row["key"] for row in result["strategies"]] == ["daily"]
    assert not any(item["key"] == "daily_vs_monthly_cash" for item in result["strategies"][0]["thresholds"])


def test_displayed_daily_threshold_also_funds_every_interim_month_from_same_budget():
    inputs = daily(purchase_price=11_993_000, exit_value=11_993_000,
                   repair_cost=0, buy_cost=0, annual_owner_cost=120_000,
                   wait_months=0.99, adr=20_000, occupancy_pct=60,
                   stays_per_month=0, cleaning_cost_per_stay=0,
                   platform_fee_pct=0, management_fee_pct=0, sell_cost_pct=0)
    original = calculate(budget=12_000_000, daily=inputs)
    row = original['strategies'][0]
    assert row['minimum_cash_balance'] == pytest.approx(650)
    threshold = row['break_even_occupancy_pct']
    assert 49 < threshold < 50  # Economic equality alone (~30%) lacks cash in month one.
    funded = calculate(budget=12_000_000, daily={**inputs, 'occupancy_pct': threshold})
    assert len(funded['strategies']) == 1
    assert funded['strategies'][0]['minimum_cash_balance'] >= 0
    assert funded['strategies'][0]['total_profit'] >= funded['banks'][0]['total_profit']
    unfunded = calculate(budget=12_000_000, daily={**inputs, 'occupancy_pct': threshold - 0.1})
    assert unfunded['strategies'] == []
    assert unfunded['diagnostics'][0]['status'] == 'insufficient_reserve'


def test_finite_but_extremely_large_occupancy_ratio_cannot_overflow_rounding():
    result = calculate(monthly=monthly(), daily=daily(adr=1e-302))
    row = next(item for item in result['strategies'] if item['key'] == 'daily')
    assert row['break_even_occupancy_pct'] is None
    assert row['break_even_cash_occupancy_pct'] is None
    json.dumps(result, allow_nan=False)
