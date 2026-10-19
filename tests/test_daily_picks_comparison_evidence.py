"""Short public reasons must remain supported by validated numeric evidence."""
import copy
from datetime import datetime, timezone

import pytest

from bot.core import daily_picks as dp


NOW = datetime(2026, 10, 13, 4, tzinfo=timezone.utc)
PHOTO = "https://krisha-photos.kcdn.online/webp/c7/c70532b0-9310-4f48-bc2d-e59a83f9f345/5-full.jpg"


def verified_snapshot():
    rows = [dict(id=str(1016000000 + index), url=f"https://krisha.kz/a/show/{1016000000 + index}",
        property_id=index, price=24_000_000 if index == 0 else 30_000_000,
        area=60, rooms=2, floor=5, floors_total=12, is_active=True,
        market_type="secondary", last_seen=NOW, complex_id=1,
        complex_resolution="name", canonical_complex_name="Тестовый ЖК",
        approved_photo_urls=[PHOTO], finish_level="finished", ceiling_height=3.0, kitchen_area=14,
        identity_checked=True, identity_complex_id=1, identity_complex_name="Тестовый ЖК",
        identity_complex_valid=True, identity_complex_is_umbrella=False, identity_complex_parent_id=None,
        resolved_house_id=None, identity_house_id=None, identity_house_name=None,
        identity_house_valid=False, identity_house_is_umbrella=False, identity_house_parent_id=None,
        identity_complex_review_pending=False, identity_property_match_pending=False,
        identity_name_ambiguous=False, identity_source_name_conflict=False) for index in range(8)]
    snapshot = dp.build_daily_picks(rows, NOW)
    for item in snapshot["items"]:
        item.update(source_status="active", source_checked_at=NOW.isoformat())
    return snapshot


@pytest.mark.parametrize("change", [
    {"comparable_count": 5.5}, {"comparable_count": "7"}, {"comparable_count": True}, {"comparable_count": 4},
    {"discount_pct": True}, {"discount_pct": "20"}, {"discount_pct": float("nan")},
    {"discount_pct": float("inf")}, {"discount_pct": 31}, {"discount_pct": -6},
    {"reference_price_per_m2": 0}, {"reference_price_per_m2": -500_000},
    {"reference_price_per_m2": True}, {"reference_price_per_m2": "500000"},
    {"reference_price_per_m2": float("nan")}, {"reference_price_per_m2": float("inf")},
    {"scope": "по всему городу"}, {"scope": None}, {"scope": ["в том же ЖК"]},
])
def test_invalid_comparison_fields_cannot_generate_public_price_claim(change):
    snapshot = verified_snapshot()
    snapshot["items"][0]["price_comparison"].update(change)
    with pytest.raises(ValueError):
        dp.validate_snapshot(snapshot)


def test_discount_claim_must_match_asking_price_and_stored_reference():
    snapshot = verified_snapshot()
    snapshot["items"][0]["price_comparison"]["discount_pct"] = 19
    with pytest.raises(ValueError, match="contradicts its price evidence"):
        dp.validate_snapshot(snapshot)


def test_matching_discount_ratio_cannot_hide_wrong_asking_price_per_meter():
    snapshot = verified_snapshot()
    snapshot["items"][0]["price_per_m2"] = 500_000
    snapshot["items"][0]["price_comparison"]["reference_price_per_m2"] = 625_000
    with pytest.raises(ValueError, match="contradicts the listing"):
        dp.validate_snapshot(snapshot)


def test_missing_median_cannot_generate_public_price_claim():
    snapshot = verified_snapshot()
    del snapshot["items"][0]["price_comparison"]["reference_price_per_m2"]
    with pytest.raises(ValueError):
        dp.validate_snapshot(snapshot)


def test_nearby_short_copy_retains_exact_radius_and_uncertainty_without_changing_proof():
    snapshot = verified_snapshot()
    item = snapshot["items"][0]
    item["price_comparison"]["scope"] = "в радиусе 800 м по приблизительным координатам"
    item["cautions"] = []
    snapshot["items"][0]["seller_phone"] = "PRIVATE-PHONE"
    original = copy.deepcopy(snapshot)
    safe = dp.validate_snapshot(snapshot)
    card = safe["items"][0]
    assert len(card["reasons"]) == 4
    assert "предложений рядом" in card["reasons"][0]
    assert any("800 м" in caution and "приблизительные" in caution for caution in card["cautions"])
    assert card["price_comparison"] == original["items"][0]["price_comparison"]
    assert card["source_checked_at"] == original["items"][0]["source_checked_at"]
    assert card["observed_at"] == original["items"][0]["observed_at"]
    assert safe["generated_at"] == original["generated_at"]
    assert safe["selection_date"] == original["selection_date"]
    assert card["rank"] == original["items"][0]["rank"]
    assert "seller_phone" not in card
    assert snapshot == original
    assert dp.validate_snapshot(safe) == safe


def test_rounded_public_area_and_median_keep_valid_price_evidence():
    snapshot = verified_snapshot()
    item = snapshot["items"][0]
    actual_area = 60.04
    item["area"] = 60.0
    item["price_per_m2"] = round(item["price"] / actual_area)
    item["price_comparison"]["reference_price_per_m2"] = 500_000
    item["price_comparison"]["discount_pct"] = round((1 - item["price"] / actual_area / 500_000) * 100, 1)
    assert dp.validate_snapshot(snapshot)["items"][0]["price_comparison"] == item["price_comparison"]


@pytest.mark.parametrize("discount", [-1, 0, 1, 2.9])
def test_public_validator_rejects_parity_premium_or_small_discount_with_four_other_facts(discount):
    snapshot = verified_snapshot()
    item = snapshot["items"][0]
    item["price"] = round(500_000*item["area"]*(1-discount/100))
    item["price_per_m2"] = round(item["price"]/item["area"])
    item["price_comparison"].update(discount_pct=discount, reference_price_per_m2=500_000)
    item["selection_facts"].update(is_owner=True, owner_source_verified=True)
    assert dp.validate_snapshot(snapshot)["items"] == []
