"""Stage-3 arithmetic is pure: explicit scenarios, no tax/legal assumptions."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from bot.core.sublease import (SubleaseParams, SubleaseError, sublease_economics,
    scenario_economics, params_from_inputs, occupancy_scenarios, mid_term_economics, evaluate_sublease_alert)


def params(**overrides):
    return SubleaseParams(**({"occupancy": .6, "avg_stay_nights": 2.5, "cleaning_per_turn": 3000,
        "consumables_per_night": 500, "utilities_month": 20000, "platform_fee_pct": .10, "tax_pct": .03,
        "furnish_capex": 600000, "capex_months": 24, "deposit_months": 1} | overrides))


def inputs(**overrides):
    from dataclasses import asdict
    return asdict(params()) | overrides


def test_hand_calculation_uses_30_4_and_all_declared_costs():
    result = sublease_economics(200000, 20000, "none", params())
    assert result["days_per_month"] == 30.4
    assert result["nights"] == 18.24 and result["turns"] == 7.296
    assert result["revenue"] == 364800 and result["costs"] == 323432
    assert result["margin"] == 41368 and result["startup_capital"] == 1000000
    assert result["breakeven_occupancy"] == pytest.approx(245000 / (15700 * 30.4), abs=0.000001)
    assert result["params_used"] == inputs()
    assert result["index_source"] == {"kind": "manual"} and result["scenario_only"] is True


def test_at_breakeven_margin_is_within_money_rounding():
    result = sublease_economics(200000, 20000, "none", params())
    at_break = sublease_economics(200000, 20000, "none", params(occupancy=result["breakeven_occupancy"]))
    assert abs(at_break["margin"]) <= 1


def test_full_furnishing_zeros_both_amortisation_and_startup_capex():
    full = sublease_economics(200000, 20000, "full", params())
    zero = sublease_economics(200000, 20000, "none", params(furnish_capex=0))
    assert full["costs"] == zero["costs"] and full["startup_capital"] == zero["startup_capital"] == 400000
    assert full["params_used"]["furnish_capex"] == 0


def test_refundable_deposit_changes_startup_only():
    first = sublease_economics(200000, 20000, "none", params())
    second = sublease_economics(200000, 20000, "none", params(deposit_months=3))
    assert first["margin"] == second["margin"] and first["costs"] == second["costs"]
    assert second["startup_capital"] - first["startup_capital"] == 400000
    assert second["deposit_is_expense"] is False


def test_zero_revenue_nonpositive_night_contribution_and_impossible_threshold():
    empty = sublease_economics(200000, 20000, "none", params(occupancy=0))
    assert empty["revenue"] == 0 and empty["margin_pct"] is None and empty["payback_months"] is None
    no_contribution = sublease_economics(200000, 100, "full", params())
    assert no_contribution["breakeven_occupancy"] is None and no_contribution["breakeven_possible"] is False
    impossible = sublease_economics(2000000, 20000, "full", params())
    assert impossible["breakeven_occupancy"] > 1 and impossible["breakeven_possible"] is False


@pytest.mark.parametrize("field,value", [("occupancy", -1), ("occupancy", 1.01),
    ("avg_stay_nights", 0), ("capex_months", 0), ("capex_months", 1.5),
    ("tax_pct", None), ("tax_pct", "NaN"), ("tax_pct", float("inf")),
    ("tax_pct", True), ("cleaning_per_turn", -1), ("furnish_capex", True),
    ("utilities_month", "1e99"), ("deposit_months", -1)])
def test_invalid_params_are_rejected(field, value):
    with pytest.raises(SubleaseError):
        params(**{field: value})


@pytest.mark.parametrize("value", [True, None, 0, -1, "nan", "inf", 1.2, "1e99", {}, []])
def test_invalid_rent_is_not_coerced(value):
    with pytest.raises(SubleaseError):
        sublease_economics(value, 20000, "full", params())


def test_combined_rates_and_unknown_furnishing_are_rejected():
    with pytest.raises(SubleaseError):
        params(platform_fee_pct=.8, tax_pct=.3)
    with pytest.raises(SubleaseError):
        sublease_economics(200000, 20000, "furnished", params())


def test_default_and_configured_occupancy_scenarios_are_explicit():
    results = scenario_economics(200000, 20000, "none", params())
    assert [r["params_used"]["occupancy"] for r in results] == [.45, .6, .75]
    custom = occupancy_scenarios({"SUBLEASE_OCC_SCENARIOS": "0.25,0.5,0.8"})
    assert custom == (.25, .5, .8)
    for bad in ("nan", ".6,.6", "-1,.5", ".1,.2,.3,.4,.5,.6"):
        with pytest.raises(SubleaseError):
            occupancy_scenarios({"SUBLEASE_OCC_SCENARIOS": bad})


def test_tax_requires_user_value_or_complete_operator_receipt():
    supplied = inputs()
    supplied.pop("tax_pct")
    with pytest.raises(SubleaseError) as error:
        params_from_inputs(supplied, {"SUBLEASE_TAX_PCT": "0", "SUBLEASE_TAX_VERIFIED": "1"})
    assert "tax_pct" in error.value.errors
    settings = {"SUBLEASE_TAX_PCT": "0.04", "SUBLEASE_TAX_VERIFIED": "1",
                "SUBLEASE_TAX_CHECKED_AT": "2026-01-01T00:00:00+00:00", "SUBLEASE_TAX_REFERENCE_HASH": "a"*64}
    p, sources = params_from_inputs(supplied, settings)
    assert p.tax_pct == .04 and sources["tax_pct"] == "operator_setting"
    explicit, _ = params_from_inputs(supplied | {"tax_pct": 0}, settings)
    assert explicit.tax_pct == 0
    for field in ("SUBLEASE_TAX_CHECKED_AT", "SUBLEASE_TAX_REFERENCE_HASH", "SUBLEASE_TAX_PCT"):
        with pytest.raises(SubleaseError):
            params_from_inputs(supplied, settings | {field: ""})


def test_missing_costs_are_not_invented_and_full_capex_is_a_rule():
    fields = inputs()
    fields.pop("utilities_month")
    with pytest.raises(SubleaseError):
        params_from_inputs(fields, {})
    p, _ = params_from_inputs(fields, {"SUBLEASE_UTILITIES_BY_ROOMS": '{"2":35000}'}, rooms=2)
    assert p.utilities_month == 35000
    full = inputs()
    full.pop("furnish_capex")
    p, _ = params_from_inputs(full, {}, furnishing="full")
    assert p.furnish_capex == 0


def test_midterm_explicit_vacancy_and_turnovers_do_not_use_removed_ad_duration():
    result = mid_term_economics(200000, 350000, "none", params(), vacancy_fraction=.1, turnovers_per_year=2)
    assert result["revenue"] == 315000
    # Variable = 40950 rates + 13680 consumables. Fixed=245000 + 500 cleaning.
    assert result["costs"] == 300130 and result["margin"] == 14870
    assert result["params_used"]["vacancy_fraction"] == .1
    with pytest.raises(SubleaseError):
        mid_term_economics(200000, 350000, "none", params(), vacancy_fraction=None, turnovers_per_year=2)


def alert_candidate(**overrides):
    now = datetime.now(timezone.utc)
    economics = sublease_economics(200000, 20000, "full", params())
    economics["index_source"] = {"level": "complex", "confidence": "high", "n": 5,
        "rent_period": "daily", "policy_version": "daily_v1", "window_days": 30,
        "active_supply": 5,
        "source_oldest_at": (now-timedelta(days=10)).isoformat(),
        "source_newest_at": now.isoformat(), "updated_at": now.isoformat()}
    return {"id": "12345678", "property_id": 42, "price": 200000, "rooms": 2, "district": "Есильский",
        "is_active": True, "is_duplicate": False, "dup_needs_review": False,
        "rent_period": "monthly", "rent_period_conflict": False, "last_seen": now,
        "sublease_economics": economics, **overrides}


def test_alert_requires_verified_identity_and_qualified_daily_source():
    candidate = alert_candidate()
    result = evaluate_sublease_alert(candidate, {"breakeven_occupancy_max": .6})
    assert result["listing_id"] == "12345678" and result["scenario_only"] is True
    assert evaluate_sublease_alert(alert_candidate(property_id=None), {"breakeven_occupancy_max": .6}) is None
    assert evaluate_sublease_alert(alert_candidate(rent_period="daily"), {"breakeven_occupancy_max": .6}) is None
    candidate["sublease_economics"]["index_source"]["n"] = 1
    assert evaluate_sublease_alert(candidate, {"breakeven_occupancy_max": .6}) is None


def test_alert_threshold_uses_exact_value_not_rounded_display():
    candidate = alert_candidate()
    candidate["sublease_economics"].update(breakeven_occupancy=.5, breakeven_occupancy_exact=".5000001")
    assert evaluate_sublease_alert(candidate, {"breakeven_occupancy_max": .5}) is None
