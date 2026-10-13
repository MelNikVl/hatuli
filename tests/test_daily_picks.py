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
                 year_built=2020, ceiling_height=3, complex_id=1, complex_resolution="url",
                 canonical_complex_name="Тестовый ЖК", approved_photo_urls=[PHOTO])
    return {**value, **changes}


def rows():
    return [row(0, price=24_000_000)] + [row(i) for i in range(1, 8)]


def first(snapshot):
    return snapshot["items"][0]


def confirmed(snapshot):
    for item in snapshot["items"]:
        item.update(source_status="active", source_checked_at=snapshot["generated_at"])
    return snapshot


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
    baseline = confirmed(dp.build_daily_picks(rows(), NOW))
    refill = dp.build_daily_picks(rows(), NOW, excluded_ids={"1016000000"})
    assert "1016000000" not in [item["id"] for item in refill["items"]]
    assert first(refill)["price_comparison"]["comparable_count"] == first(baseline)["price_comparison"]["comparable_count"]


def test_nearby_fallback_exact_scope_and_rejects_distance_or_finish_mismatch():
    sample = [row(0, price=24_000_000, complex_resolution="unbound", lat=51.1, lon=71.4, finish_level="finished")]
    sample += [row(i, complex_id=i+10, lat=51.1001, lon=71.4001, finish_level="finished") for i in range(1,6)]
    sample += [row(6, complex_id=20, lat=51.13, lon=71.4), row(7, complex_id=21, lat=51.1001, lon=71.4001, finish_level="shell")]
    item = first(dp.build_daily_picks(sample, NOW))
    assert item["price_comparison"]["scope"] == "в радиусе 800 м по приблизительным координатам"
    assert item["price_comparison"]["comparable_count"] == 5
    assert "похожих предложений рядом" in item["reasons"][0]
    assert "800" not in " ".join(item["reasons"])
    assert not any("аналогов" in reason or "±15%" in reason for reason in item["reasons"])
    assert any("800 м" in caution for caution in item["cautions"])
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


def test_near_median_premium_requires_strong_fact_not_middle_floor():
    sample = rows()
    sample[0].update(price=31_000_000, floor=1, ceiling_height=None)
    assert "1016000000" not in [item["id"] for item in dp.build_daily_picks(sample, NOW)["items"]]
    sample[0]["floor"] = 5
    assert "1016000000" not in [item["id"] for item in dp.build_daily_picks(sample, NOW)["items"]]
    sample[0]["ceiling_height"] = 3
    result = next(item for item in dp.build_daily_picks(sample, NOW)["items"] if item["id"] == "1016000000")
    assert "выше похожих предложений" in result["reasons"][0]
    assert result["price_comparison"]["discount_pct"] < 0


def test_floor_caution_survives_price_cut_reason():
    sample = rows()
    sample[0].update(floor=12, price_events=[{"old_price": 27_000_000, "new_price": 24_000_000, "changed_at": NOW-timedelta(days=5)}])
    item = first(dp.build_daily_picks(sample, NOW))
    assert item["price_history"]["net_change"] == -3_000_000
    assert item["price_history"]["events"][0]["direction"] == "down"
    assert "Последний этаж" in item["cautions"][0]


def test_price_cut_not_reported_if_history_outdated():
    sample = rows()
    sample[0].update(price_events=[{"old_price": 27_000_000, "new_price": 25_000_000, "changed_at": NOW-timedelta(days=5)}])
    assert not any("Снижение цены" in reason for reason in first(dp.build_daily_picks(sample, NOW))["reasons"])


def test_recorded_recent_price_reduction_qualifies_market_price_without_other_strong_facts():
    sample = rows()
    sample[0].update(price=30_000_000, ceiling_height=None,
        price_events=[{"old_price": 32_000_000, "new_price": 30_000_000, "changed_at": NOW-timedelta(days=7)}])
    item = next(item for item in dp.build_daily_picks(sample, NOW)["items"] if item["id"] == "1016000000")
    assert "на уровне" in item["reasons"][0]
    assert item["price_history"]["net_change"] == -2_000_000
    assert item["price_history"]["changes_count"] == 1


def test_duplicate_or_broken_history_is_not_a_reason_to_select_market_price():
    sample = rows()
    sample[0].update(price=30_000_000, ceiling_height=None, price_events=[
        {"old_price": 32_000_000, "new_price": 30_000_000, "changed_at": NOW-timedelta(days=7)},
        {"old_price": 32_000_000, "new_price": 30_000_000, "changed_at": NOW-timedelta(days=2)}])
    assert "1016000000" not in [item["id"] for item in dp.build_daily_picks(sample, NOW)["items"]]


@pytest.mark.parametrize("finish", ["finished", "renovated", "designer", "needs_repair"])
def test_repair_category_alone_cannot_qualify_a_market_priced_pick(finish):
    sample = [{**item, "finish_level": finish} for item in rows()]
    sample[0].update(price=30_000_000, ceiling_height=None)
    assert "1016000000" not in [item["id"] for item in dp.build_daily_picks(sample, NOW)["items"]]
    sample[0]["price"] = 24_000_000
    item = next(item for item in dp.build_daily_picks(sample, NOW)["items"] if item["id"] == "1016000000")
    assert any("ниже похожих" in reason for reason in item["reasons"])
    assert not any("ремонт" in reason.lower() or "отделк" in reason.lower() for reason in item["reasons"])


def test_legacy_snapshot_remains_visible_but_unsupported_reasons_are_removed():
    data = confirmed(dp.build_daily_picks(rows(), NOW))
    for item in data["items"]:
        item.pop("selection_facts")
        item.pop("price_history")
        item["reasons"] = ["Цена за м² ниже рынка.", "7 аналогов: столько же комнат, площадь ±15%.", "Квартира срочно продаётся, коммуналка низкая."]
    safe = dp.validate_snapshot(data)
    assert len(safe["items"]) == len(data["items"])
    assert safe["generated_at"] == data["generated_at"] and safe["selection_date"] == data["selection_date"]
    for old, new in zip(data["items"], safe["items"]):
        assert new["rank"] == old["rank"] and new["id"] == old["id"]
        assert new["source_checked_at"] == old["source_checked_at"]
        assert len(new["reasons"]) == 1
        assert "срочно" not in new["reasons"][0] and "коммунал" not in new["reasons"][0]


def test_personal_or_confidential_fields_not_projected():
    sample = rows()
    sample[0].update(title="Seller 87071234567", address="Street 15 apartment 24", seller_name="Private Name", phone="87071234567", description="Private document", investigation_notes="secret", complex_name="RAW PHONE 87071234567", district="Street 123 apt 12")
    item = first(dp.build_daily_picks(sample, NOW))
    encoded = json.dumps(item)
    assert all(word not in encoded for word in ("87071234567", "apartment 24", "Private", "secret", "RAW"))
    assert item["address"] == "Street 15"
    assert item["district"] == "Астана" and item["complex_name"] == "Тестовый ЖК"


def test_budget_is_inclusive_and_does_not_remove_more_expensive_comparables():
    sample = [row(0, price=100_000_000, area=100)] + [row(i, price=110_000_000, area=100) for i in range(1, 8)]
    snapshot = dp.build_daily_picks(sample, NOW)
    assert [item["id"] for item in snapshot["items"]] == ["1016000000"]
    assert first(snapshot)["price_comparison"]["comparable_count"] == 7
    assert first(snapshot)["price_comparison"]["reference_price_per_m2"] == 1_100_000
    sample[0]["price"] = 100_000_001
    assert dp.build_daily_picks(sample, NOW)["items"] == []


def test_legacy_luxury_cards_are_removed_without_discarding_verified_edition():
    snapshot = confirmed(dp.build_daily_picks(rows(), NOW))
    snapshot["items"][1]["price"] = 120_000_000
    original = copy.deepcopy(snapshot)
    safe = dp.validate_snapshot(snapshot)
    assert len(safe["items"]) == len(snapshot["items"]) - 1
    assert [item["rank"] for item in safe["items"]] == list(range(1, len(safe["items"]) + 1))
    assert snapshot["items"][1]["id"] not in [item["id"] for item in safe["items"]]
    assert safe["generated_at"] == snapshot["generated_at"]
    assert safe["source_observed_at"] == snapshot["source_observed_at"]
    assert all(item["source_checked_at"] == snapshot["generated_at"] for item in safe["items"])
    assert snapshot == original
    assert dp.validate_snapshot(safe) == safe


def test_all_luxury_legacy_cards_are_hidden_but_empty_edition_cannot_overwrite(tmp_path):
    snapshot = confirmed(dp.build_daily_picks(rows(), NOW))
    path = tmp_path / "daily.json"
    dp.publish_snapshot(snapshot, path)
    original = path.read_bytes()
    for item in snapshot["items"]:
        item["price"] = 120_000_000
    safe = dp.validate_snapshot(snapshot)
    assert safe["items"] == [] and dp.validate_snapshot(safe) == safe
    with pytest.raises(ValueError, match="empty edition"):
        dp.publish_snapshot(safe, path)
    assert path.read_bytes() == original


@pytest.mark.parametrize("address,expected", [
    ("ул. Кунаева 12 — Продам, звоните +7 707 1234567", "ул. Кунаева 12"),
    ("  пр. Туран 34, кв. 18  ", "пр. Туран 34"),
    ("ул. Туран 34 apartment 18", "ул. Туран 34"),
    ("ул. Туран 34, телефон +7 (707) 123-45-67", None),
    ("ул. Туран 34, seller@example.com", None),
    (None, None),
])
def test_source_street_house_address_is_public_but_contacts_and_flat_are_not(address, expected):
    sample = rows()
    sample[0]["address"] = address
    snapshot = confirmed(dp.build_daily_picks(sample, NOW))
    assert first(snapshot)["address"] == expected
    assert first(dp.validate_snapshot(snapshot))["address"] == expected
    assert first(snapshot)["address_note"] == "Адрес из объявления." if expected else "не указан" in first(snapshot)["address_note"]


@pytest.mark.parametrize("address,note", [
    ("Есильский р-н, Ұлы дала 12", "Адрес из объявления."),
    ("Есильский р-н, Ұлы дала", "Номер дома не указан в объявлении."),
    ("Есильский р-н, 38 улица", "Номер дома не указан в объявлении."),
    ("Есильский р-н, 38 улица 12", "Адрес из объявления."),
    ("Есильский р-н, Толе би 25, 25А, 14, 14/1", "В объявлении указано несколько домов; точный дом нужно уточнить."),
    ("Есильский р-н, Толе би25,25А,14,14/1", "В объявлении указано несколько домов; точный дом нужно уточнить."),
    ("Есильский р-н, Керей, Жанибек хандар 2, 2/1", "В объявлении указано несколько домов; точный дом нужно уточнить."),
    ("Есильский р-н, Керей, Жанибек хандар2,2/1", "В объявлении указано несколько домов; точный дом нужно уточнить."),
    ("Есильский р-н, Керей, Жанибек хандар 2/1", "Адрес из объявления."),
    ("Есильский р-н, 12", "Адрес неполный; точный дом нужно уточнить."),
])
def test_address_note_distinguishes_a_single_house_missing_house_and_house_list(address, note):
    safe_address, safe_note = dp.sanitized_listing_address(address, 30_000_000, NOW)
    assert safe_address == address
    assert safe_note == note


def test_legacy_address_can_be_absent_and_untrusted_note_is_recomputed():
    snapshot = confirmed(dp.build_daily_picks(rows(), NOW))
    snapshot["items"][0].pop("address")
    snapshot["items"][0]["address_note"] = "Private contact +7 707 1234567"
    safe = dp.validate_snapshot(snapshot)
    assert first(safe)["address"] is None
    assert first(safe)["address_note"] == "Точный адрес не указан в источнике."


@pytest.mark.parametrize("url", ["http://krisha-photos.kcdn.online/1.jpg", "https://krisha-photos.kcdn.online.evil.test/a.jpg", "https://evil@krisha-photos.kcdn.online/a.jpg", PHOTO+"?token=private", PHOTO+"#x", PHOTO.replace("https://", "https://") .replace(".online/", ".online:444/"), "https://krisha-photos.kcdn.online/docs/passport.jpg"])
def test_photo_allowlist_rejects_untrusted_urls(url):
    assert dp.safe_photo_url(url) is None


def test_atomic_failure_keeps_last_good_snapshot(tmp_path, monkeypatch):
    path = tmp_path / "daily.json"
    baseline = confirmed(dp.build_daily_picks(rows(), NOW))
    dp.publish_snapshot(baseline, path)
    content = path.read_bytes()
    def fail(*args):
        raise OSError("simulated interrupted publish")
    monkeypatch.setattr(dp.os, "replace", fail)
    with pytest.raises(OSError):
        dp.publish_snapshot(confirmed(dp.build_daily_picks(rows(), NOW+timedelta(hours=1))), path)
    assert path.read_bytes() == content
    assert list(tmp_path.iterdir()) == [path]


def test_validation_fail_never_overwrites_previous_snapshot(tmp_path):
    path = tmp_path / "daily.json"
    snapshot = confirmed(dp.build_daily_picks(rows(), NOW))
    dp.publish_snapshot(snapshot, path)
    content = path.read_bytes()
    snapshot["items"][0]["source_url"] = "javascript:alert(1)"
    with pytest.raises(ValueError):
        dp.publish_snapshot(snapshot, path)
    assert path.read_bytes() == content


def test_load_strips_extra_fields_and_fails_closed(tmp_path):
    path = tmp_path / "daily.json"
    snapshot = confirmed(dp.build_daily_picks(rows(), NOW))
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


def test_old_unverified_snapshot_fails_closed(tmp_path):
    path = tmp_path / "daily.json"
    snapshot = dp.build_daily_picks(rows(), NOW)
    snapshot["schema_version"] = 1
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    assert dp.load_daily_picks(path) is None
    snapshot["schema_version"] = 2
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    assert dp.load_daily_picks(path) is None


def test_stale_or_nonactive_source_confirmation_rejected():
    snapshot = confirmed(dp.build_daily_picks(rows(), NOW))
    snapshot["items"][0]["source_checked_at"] = (NOW-timedelta(hours=3)).isoformat()
    with pytest.raises(ValueError):
        dp.validate_snapshot(snapshot)
    snapshot["items"][0].update(source_checked_at=NOW.isoformat(), source_status="archived")
    with pytest.raises(ValueError):
        dp.validate_snapshot(snapshot)


def test_existing_verified_snapshot_copy_refresh_keeps_facts_and_source_dates():
    snapshot = confirmed(dp.build_daily_picks(rows(), NOW))
    snapshot["items"][0]["reasons"][:2] = ["Цена за м² на 20% ниже медианы в том же ЖК.", "Сравнение с 7 объявлениями: столько же комнат, площадь ±15%."]
    item = dp.validate_snapshot(snapshot)["items"][0]
    assert "20% ниже похожих предложений в этом ЖК" in item["reasons"][0]
    assert "6 млн" in item["reasons"][0]
    assert not any("аналогов" in text or "±15%" in text for text in item["reasons"])
    assert item["price_comparison"] == snapshot["items"][0]["price_comparison"]
    assert item["source_checked_at"] == snapshot["items"][0]["source_checked_at"]
    assert item["rank"] == snapshot["items"][0]["rank"]


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
