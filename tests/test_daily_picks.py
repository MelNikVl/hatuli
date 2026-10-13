"""Pure/offline regressions: no database URL, pool, or production fixtures."""
import copy
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot.core import daily_picks as dp

NOW = datetime(2026, 10, 13, 4, tzinfo=timezone.utc)
PHOTO = "https://krisha-photos.kcdn.online/webp/c7/c70532b0-9310-4f48-bc2d-e59a83f9f345/5-full.jpg"


def row(index=0, **changes):
    value = dict(id=str(1016000000 + index), url=f"https://krisha.kz/a/show/{1016000000 + index}", property_id=index, price=30_000_000,
                 area=60., rooms=2, floor=5, floors_total=12, district="Есильский р-н",
                 is_active=True, is_duplicate=False, dup_needs_review=False,
                 market_type="secondary", last_seen=NOW - timedelta(hours=2),
                 year_built=2020, complex_id=1, complex_resolution="url",
                 canonical_complex_name="Тестовый ЖК", approved_photo_urls=[PHOTO])
    return {**value, **changes}


def rows():
    return [row(0, price=24_000_000)] + [row(i) for i in range(1, 8)]


def first(snapshot):
    return snapshot["items"][0]


def test_fresh_median_correct_discount_not_upside_or_stale_score():
    sample = rows()
    sample[0].update(score_total=100, hex_details={"di": 2, "expected_m2": 1_000_000})
    result = first(dp.build_daily_picks(sample, NOW))
    assert result["price_comparison"] == dict(discount_pct=20., reference_price_per_m2=500_000, scope="в том же ЖК", comparable_count=7)
    assert "20% ниже" in result["reasons"][0]
    assert result["price_per_m2"] == 400_000


@pytest.mark.parametrize("change", [dict(is_active=False), dict(is_active=None), dict(is_duplicate=True),
    dict(dup_needs_review=True), dict(market_type="primary"), dict(market_type="rent"),
    dict(price=0), dict(area=float("nan")), dict(area=10), dict(rooms=1.5),
    dict(last_seen=NOW-timedelta(hours=73)), dict(last_seen=NOW+timedelta(seconds=1)),
    dict(last_seen=datetime(2026,10,13)), dict(year_built=2027), dict(complex_resolution="geo"),
    dict(id="https://evil.example/"), dict(floor=None), dict(floor=13), dict(approved_photo_urls=[]),
    dict(url="https://evil.example/a/show/1016000000"), dict(url="https://krisha.kz/a/show/1016000001"),
])
def test_invalid_or_unreliable_card_excluded(change):
    sample = rows()
    sample[0].update(change)
    assert "1016000000" not in [item["id"] for item in dp.build_daily_picks(sample, NOW)["items"]]


def test_at_least_five_unique_comparables_and_area_band():
    sample = [row(0, price=24_000_000)] + [row(i) for i in range(1,5)] + [row(7, area=80), row(8, rooms=3)]
    assert not dp.build_daily_picks(sample, NOW)["items"]
    sample.append(row(9))
    assert first(dp.build_daily_picks(sample, NOW))["price_comparison"]["comparable_count"] == 5


def test_known_property_duplicates_never_inflate_count_or_publish_twice():
    sample = [row(0, price=24_000_000)] + [row(i, property_id=1) for i in range(1,6)]
    assert not dp.build_daily_picks(sample, NOW)["items"]
    sample = rows() + [row(100, property_id=0, price=24_000_000, last_seen=NOW-timedelta(hours=1))]
    ids = [item["id"] for item in dp.build_daily_picks(sample, NOW)["items"]]
    assert "1016000000" not in ids and "1016000100" in ids


def test_excluded_candidate_retained_as_comparable_for_refill():
    baseline = dp.build_daily_picks(rows(), NOW)
    refill = dp.build_daily_picks(rows(), NOW, excluded_ids={"1016000000"})
    assert "1016000000" not in [item["id"] for item in refill["items"]]
    assert first(refill)["price_comparison"]["comparable_count"] == first(baseline)["price_comparison"]["comparable_count"]


def test_nearby_fallback_exact_scope_and_rejects_distance_or_finish_mismatch():
    sample = [row(0, price=24_000_000, complex_resolution="unbound", lat=51.1, lon=71.4, finish_level="finished")]
    sample += [row(i, complex_id=i+10, lat=51.1001, lon=71.4001, finish_level="finished") for i in range(1,6)]
    sample += [row(6, complex_id=20, lat=51.13, lon=71.4), row(7, complex_id=21, lat=51.1001, lon=71.4001, finish_level="shell")]
    item = first(dp.build_daily_picks(sample, NOW))
    assert item["price_comparison"]["scope"] == "в радиусе 800 м по координатам объявлений"
    assert item["price_comparison"]["comparable_count"] == 5
    assert "в радиусе 800 м" in item["reasons"][0]
    assert any("приблизительные" in caution for caution in item["cautions"])


def test_known_unfinished_condition_has_individual_caution():
    sample = rows()
    sample[0]["finish_level"] = "rough"
    assert any("расходы на ремонт" in caution for caution in first(dp.build_daily_picks(sample, NOW))["cautions"])


def test_invalid_or_outside_astana_coords_cannot_create_nearby_comparison():
    sample = [row(i, complex_resolution="unbound", lat=float("nan"), lon=71.4) for i in range(8)]
    assert not dp.build_daily_picks(sample, NOW)["items"]
    for entry in sample:
        entry.update(lat=43.25, lon=76.9)
    assert not dp.build_daily_picks(sample, NOW)["items"]


def test_geo_house_attribution_does_not_claim_same_physical_house():
    sample = [row(i, resolved_house_id=99, house_attribution="geo") for i in range(8)]
    assert first(dp.build_daily_picks(sample, NOW))["price_comparison"]["scope"] == "в том же ЖК"


def test_extreme_discount_excluded_not_celebrated():
    sample = rows()
    sample[0]["price"] = 15_000_000
    assert "1016000000" not in [item["id"] for item in dp.build_daily_picks(sample, NOW)["items"]]


def test_near_median_premium_requires_middle_floor():
    sample = rows()
    sample[0].update(price=31_000_000, floor=1)
    assert "1016000000" not in [item["id"] for item in dp.build_daily_picks(sample, NOW)["items"]]
    sample[0]["floor"] = 5
    result = next(item for item in dp.build_daily_picks(sample, NOW)["items"] if item["id"] == "1016000000")
    assert "выше медианы" in result["reasons"][0]
    assert result["price_comparison"]["discount_pct"] < 0


def test_floor_caution_survives_price_cut_reason():
    sample = rows()
    sample[0].update(floor=12, first_price_30d=27_000_000, latest_history_price=24_000_000)
    item = first(dp.build_daily_picks(sample, NOW))
    assert "снизилась" in item["reasons"][2]
    assert "Последний этаж" in item["cautions"][0]


def test_price_cut_not_reported_if_history_outdated():
    sample = rows()
    sample[0].update(first_price_30d=27_000_000, latest_history_price=25_000_000)
    assert not any("снизилась" in reason for reason in first(dp.build_daily_picks(sample, NOW))["reasons"])


def test_personal_or_confidential_fields_not_projected():
    sample = rows()
    sample[0].update(title="Seller 87071234567", address="Street 15 apartment 24", seller_name="Private Name", phone="87071234567", description="Private document", investigation_notes="secret", complex_name="RAW PHONE 87071234567", district="Street 123 apt 12")
    item = first(dp.build_daily_picks(sample, NOW))
    encoded = json.dumps(item)
    assert all(word not in encoded for word in ("87071234567", "Street", "Private", "secret", "RAW"))
    assert item["district"] == "Астана" and item["complex_name"] == "Тестовый ЖК"


@pytest.mark.parametrize("url", ["http://krisha-photos.kcdn.online/1.jpg", "https://krisha-photos.kcdn.online.evil.test/a.jpg", "https://evil@krisha-photos.kcdn.online/a.jpg", PHOTO+"?token=private", PHOTO+"#x", PHOTO.replace("https://", "https://") .replace(".online/", ".online:444/"), "https://krisha-photos.kcdn.online/docs/passport.jpg"])
def test_photo_allowlist_rejects_untrusted_urls(url):
    assert dp.safe_photo_url(url) is None


def test_atomic_failure_keeps_last_good_snapshot(tmp_path, monkeypatch):
    path = tmp_path / "daily.json"
    baseline = dp.build_daily_picks(rows(), NOW)
    dp.publish_snapshot(baseline, path)
    content = path.read_bytes()
    def fail(*args):
        raise OSError("simulated interrupted publish")
    monkeypatch.setattr(dp.os, "replace", fail)
    with pytest.raises(OSError):
        dp.publish_snapshot(dp.build_daily_picks(rows(), NOW+timedelta(hours=1)), path)
    assert path.read_bytes() == content
    assert list(tmp_path.iterdir()) == [path]


def test_validation_fail_never_overwrites_previous_snapshot(tmp_path):
    path = tmp_path / "daily.json"
    snapshot = dp.build_daily_picks(rows(), NOW)
    dp.publish_snapshot(snapshot, path)
    content = path.read_bytes()
    snapshot["items"][0]["source_url"] = "javascript:alert(1)"
    with pytest.raises(ValueError):
        dp.publish_snapshot(snapshot, path)
    assert path.read_bytes() == content


def test_load_strips_extra_fields_and_fails_closed(tmp_path):
    path = tmp_path / "daily.json"
    snapshot = dp.build_daily_picks(rows(), NOW)
    snapshot.update(database_password="secret")
    snapshot["items"][0].update(seller_phone="secret")
    snapshot["items"][0]["price_comparison"]["seller_phone"] = "secret"
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    assert "secret" not in json.dumps(dp.load_daily_picks(path))
    path.write_text("{broken", encoding="utf-8")
    assert dp.load_daily_picks(path) is None


@pytest.mark.parametrize("bad", [[], None, ["not a snapshot"]])
def test_load_json_wrong_shape_fails_closed(tmp_path, bad):
    path = tmp_path / "daily.json"
    path.write_text(json.dumps(bad), encoding="utf-8")
    assert dp.load_daily_picks(path) is None


def test_local_selection_date_uses_almaty_not_utc():
    now = datetime(2026,10,12,20,tzinfo=timezone.utc)
    sample = [row(i, last_seen=now-timedelta(hours=1)) for i in range(8)]
    assert dp.build_daily_picks(sample, now)["selection_date"] == "2026-10-13"


def test_snapshot_stale_after_missed_8am_refresh():
    yesterday = NOW-timedelta(days=1)
    sample = [row(i, last_seen=yesterday-timedelta(hours=1)) for i in range(8)]
    snapshot = dp.build_daily_picks(sample, yesterday)
    assert not dp.snapshot_status(snapshot, datetime(2026,10,13,2,tzinfo=timezone.utc))["is_stale"]
    assert dp.snapshot_status(snapshot, datetime(2026,10,13,3,tzinfo=timezone.utc))["is_stale"]


def test_selection_bounded_deterministic_and_input_unchanged():
    sample = [row(i) for i in range(50)]
    original = copy.deepcopy(sample)
    snapshot = dp.build_daily_picks(sample, NOW, limit=100)
    assert len(snapshot["items"]) == 30
    assert snapshot == dp.build_daily_picks(list(reversed(sample)), NOW, limit=100)
    assert sample == original


def test_publisher_reads_without_app_pool_migrations_or_updates():
    source = (Path(__file__).resolve().parents[1] / "scripts" / "publish_daily_picks.py").read_text(encoding="utf-8")
    assert "init_pool(" not in source and "apply_deal_scores(" not in source
    assert '"default_transaction_read_only": "on"' in source
    assert 'connection.transaction(isolation="repeatable_read", readonly=True)' in source
