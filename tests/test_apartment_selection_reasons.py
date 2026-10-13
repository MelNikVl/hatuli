"""Evidence-based customer copy and admission; no database or network."""
from datetime import datetime, timedelta, timezone
import copy
import json

import pytest

from bot.core.apartment_selection_reasons import (
    apartment_selection_reasons, selection_facts, strong_selection_reason,
    validate_selection_facts,
)
from bot.core.comparison_offer_details import price_history

NOW = datetime(2026, 10, 13, 4, tzinfo=timezone.utc)


def offer(**changes):
    return {"price": 24_000_000, "area": 60, "rooms": 2, "floor": 5, "floors_total": 12,
            "price_comparison": {"discount_pct": 20, "reference_price_per_m2": 500_000,
                                 "scope": "в том же ЖК", "comparable_count": 7}, **changes}


def event(old, new, days=1):
    return {"at": NOW-timedelta(days=days), "old_price": old, "new_price": new}


def test_money_percent_and_scope_help_buyer_without_methodology_filler():
    reasons = apartment_selection_reasons(offer(), NOW)
    assert len(reasons) == 1
    assert "20% ниже" in reasons[0] and "6 млн ₸" in reasons[0] and "в этом ЖК" in reasons[0]
    assert "±15%" not in " ".join(reasons) and "7 аналогов" not in " ".join(reasons)


def test_above_market_copy_is_honest_and_owner_or_middle_floor_is_not_strong():
    data = offer(price=31_000_000, price_comparison={"discount_pct": -3.3,
                 "reference_price_per_m2": 500_000, "scope": "в том же доме"}, selection_facts={"is_owner": True})
    assert not strong_selection_reason(data, NOW)
    assert "выше похожих предложений в этом доме" in apartment_selection_reasons(data, NOW)[0]


@pytest.mark.parametrize("facts,expected", [
    ({"ceiling_height": 2.9}, "Высокие потолки 2,9 м"),
    ({"kitchen_area": 12}, "Кухня 12 м²"),
    ({"finish_level": "renovated"}, "Квартира с ремонтом"),
])
def test_known_physical_fact_justifies_market_priced_selection(facts, expected):
    data = offer(selection_facts=facts)
    assert strong_selection_reason(data, NOW)
    assert any(expected in reason for reason in apartment_selection_reasons(data, NOW))


def test_ordinary_measurements_or_unknown_finish_are_not_strong():
    assert not strong_selection_reason(offer(selection_facts={"ceiling_height": 2.7, "kitchen_area": 8, "finish_level": "rough"}), NOW)


def test_repeated_declines_remain_strong_evidence_without_duplicating_history_chart():
    events = [event(27_000_000, 26_000_000, 20), event(26_000_000, 24_000_000, 5)]
    data = offer(price_history=price_history(events, 24_000_000, now=NOW))
    assert strong_selection_reason(data, NOW)
    assert apartment_selection_reasons(data, NOW) == apartment_selection_reasons(offer(), NOW)


def test_distinct_changes_on_same_day_do_not_exaggerate_repeated_cuts():
    events = [event(27_000_000, 26_000_000), event(26_000_000, 24_000_000)]
    data = offer(price_history=price_history(events, 24_000_000, now=NOW))
    assert strong_selection_reason(data, NOW)
    assert apartment_selection_reasons(data, NOW) == apartment_selection_reasons(offer(), NOW)


@pytest.mark.parametrize("events,price", [
    ([event(27_000_000, 26_000_000, 20), event(27_000_000, 24_000_000)], 24_000_000),
    ([event(27_000_000, 26_000_000)], 24_000_000),
])
def test_chain_gap_or_current_price_mismatch_suppresses_positive_history(events, price):
    history = price_history(events, price, now=NOW)
    history["status"] = "consistent"  # Do not trust forged public labels.
    data = offer(price=price, price_history=history)
    assert not strong_selection_reason(data, NOW)
    assert len(apartment_selection_reasons(data, NOW)) == 1


def test_later_price_raise_prevents_declining_price_reason():
    data = offer(price=25_000_000, price_history=price_history(
        [event(27_000_000, 24_000_000, 20), event(24_000_000, 25_000_000)], 25_000_000, now=NOW))
    assert not strong_selection_reason(data, NOW)
    assert len(apartment_selection_reasons(data, NOW)) == 1


def test_history_cut_uses_90_day_window_and_expires():
    data = offer(price_history=price_history([event(27_000_000, 24_000_000, 40)], 24_000_000, now=NOW))
    assert strong_selection_reason(data, NOW)
    assert apartment_selection_reasons(data, NOW) == apartment_selection_reasons(offer(), NOW)
    assert not strong_selection_reason(data, NOW+timedelta(days=51))


@pytest.mark.parametrize("finish,benefit", [
    ("finished", "основные отделочные работы уже выполнены"),
    ("renovated", "не нужно начинать с черновой отделки"),
    ("furnished", "часть обустройства уже есть"),
    ("designer", "интерьер уже оформлен"),
])
def test_finish_benefits_keep_source_attribution_without_inventing_quality_or_savings(finish, benefit):
    reasons = apartment_selection_reasons(offer(selection_facts={"finish_level": finish}), NOW)
    reason = reasons[-1]
    assert benefit in reason and "объявлен" in reason
    assert all(word not in reason.lower() for word in (
        "проверьте", "качествен", "хороший", "сэконом", "заехать", "сроч", "хотят продать",
    ))
    assert "₸" not in reason


def test_unknown_finish_does_not_invent_repair_benefit_from_unverified_description_or_photo():
    data = offer(description="Хороший дизайнерский ремонт", photo="https://example.com/repair.jpg")
    assert apartment_selection_reasons(data, NOW) == apartment_selection_reasons(offer(), NOW)
    assert not strong_selection_reason(data, NOW)


def test_no_synthetic_primary_baseline_decline_claim():
    data = offer(price_history=price_history([{"at": NOW-timedelta(days=3), "old_price": None, "new_price": 24_000_000}],
        24_000_000, now=NOW, baseline_unknown=True))
    assert not strong_selection_reason(data, NOW)
    assert len(apartment_selection_reasons(data, NOW)) == 1


def test_future_evidence_does_not_support_sale_argument():
    data = offer(price_history={"status": "consistent", "events": [event(27_000_000, 24_000_000, -1)]})
    assert not strong_selection_reason(data, NOW)


def test_bounded_projection_and_customer_copy_do_not_copy_raw_text_or_confidential_fields():
    raw = {"area": 60, "ceiling_height": 3.05, "kitchen_area": 15, "finish_level": "designer", "is_owner": True,
           "description": "PRIVATE PHONE", "utilities_amount": "5", "location": "PRIVATE PHONE"}
    facts = selection_facts(raw)
    assert facts == {"ceiling_height": 3.05, "kitchen_area": 15, "finish_level": "designer", "is_owner": True}
    safe = validate_selection_facts({**facts, "phone": "PRIVATE PHONE"}, 60)
    data = offer(selection_facts=safe)
    original = copy.deepcopy(data)
    reasons = apartment_selection_reasons(data, NOW)
    assert len(reasons) == 4 and all(len(reason) <= 200 for reason in reasons)
    assert "PRIVATE" not in json.dumps(safe)+json.dumps(reasons)
    assert not any("коммун" in reason or "удобная локация" in reason or "качество ЖК" in reason for reason in reasons)
    assert data == original


@pytest.mark.parametrize("facts", [
    {"ceiling_height": True}, {"ceiling_height": "3"}, {"ceiling_height": 6},
    {"kitchen_area": float("nan")}, {"kitchen_area": 50},
    {"finish_level": []}, {"finish_level": "private contact"}, {"is_owner": "yes"},
])
def test_invalid_public_measurement_fails_closed(facts):
    with pytest.raises(ValueError):
        validate_selection_facts(facts, 60)


def test_missing_or_bad_source_fields_are_omitted_without_guessing():
    assert selection_facts({"ceiling_height": 8, "kitchen_area": 900, "area": 60, "finish_level": [], "is_owner": "yes"}) == {
        "ceiling_height": None, "kitchen_area": None, "finish_level": None, "is_owner": None}
