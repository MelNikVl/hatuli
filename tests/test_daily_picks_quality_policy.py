"""Explicit quality editions use full evidence and never weaken the default quota."""
import copy
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from bot.core import daily_picks as dp
from scripts import publish_daily_picks as publisher
from scripts import publish_daily_picks_browser as browser
from tests.test_daily_picks import row


@pytest.fixture(autouse=True)
def no_live_io(monkeypatch):
    async def reject(*args, **kwargs):
        raise AssertionError("Quality-policy tests must not access DB, HTTP or a real browser")
    monkeypatch.setattr("asyncpg.connect", reject)
    monkeypatch.setattr("asyncpg.create_pool", reject)
    monkeypatch.setattr(publisher, "read_candidates", reject)
    monkeypatch.setattr(publisher, "verify_sources", reject)
    monkeypatch.setattr(browser, "ordinary_browser", reject)


def edition_rows(count=10, now=None):
    now = now or datetime.now(timezone.utc)
    return [row(index, price=24_000_000 if index < count else 30_000_000,
                floor=5 if index < count else 1, ceiling_height=3 if index < count else None,
                kitchen_area=14 if index < count else None, last_seen=now)
            for index in range(count * 2 + 6)]


def edition(count=10):
    now = datetime.now(timezone.utc)
    snapshot = dp.build_daily_picks(edition_rows(count, now), now)
    assert len(snapshot["items"]) == count
    for item in snapshot["items"]:
        item.update(source_status="active", source_checked_at=now.isoformat())
    return snapshot


def test_discounted_four_factor_policy_keeps_every_check_and_records_actual_count():
    snapshot = edition()
    before = copy.deepcopy(snapshot)
    prepared = publisher.prepare_publication(snapshot)
    assert snapshot == before
    assert len(prepared["items"]) == 10
    assert all(len(item["reasons"]) == 4 and item["complex_identity"]["checked"] is True for item in prepared["items"])
    assert prepared["publication_policy"] == {
        "name": "discounted_four_factors", "target_count": 30, "minimum_count": 10, "published_count": 10,
        "required_strong_factors": 4, "minimum_discount_pct": 3, "reason": "discount_and_four_factors", "explicit": True,
        "evaluated_at": snapshot["generated_at"],
    }
    assert dp.validate_snapshot(prepared) == prepared
    assert publisher.publication_satisfies_policy(prepared)
    assert not publisher.publication_satisfies_policy(prepared, "daily_30")


def test_default_accepts_ten_and_the_optional_full_quota_still_requires_thirty():
    with pytest.raises(RuntimeError):
        publisher.prepare_publication(edition(), "daily_30")
    full = edition(30)
    prepared = publisher.prepare_publication(full, "daily_30")
    assert len(prepared["items"]) == 30
    assert publisher.publication_satisfies_policy(prepared, "daily_30")


@pytest.mark.parametrize("policy", ["daily_30", "discounted_four_factors"])
def test_no_policy_can_publish_an_empty_edition(policy):
    snapshot = edition()
    snapshot["items"] = []
    with pytest.raises(RuntimeError):
        publisher.prepare_publication(snapshot, policy)


@pytest.mark.parametrize("change", ["missing_identity", "missing_factors", "archived", "stale_confirmation"])
def test_explicit_policy_cannot_keep_an_unverified_card_or_silently_filter_it(change):
    snapshot = edition()
    item = snapshot["items"][0]
    if change == "missing_identity":
        item["complex_identity"] = None
    elif change == "missing_factors":
        item["selection_facts"]["ceiling_height"] = None
        item["floor"] = 1
    elif change == "archived":
        item["source_status"] = "archived"
    else:
        item["source_checked_at"] = (datetime.fromisoformat(snapshot["generated_at"]) - timedelta(hours=3)).isoformat()
    with pytest.raises(ValueError):
        publisher.prepare_publication(snapshot)


@pytest.mark.parametrize("change", [
    {"name": "allow_anything"}, {"explicit": False}, {"published_count": 30}, {"published_count": True},
    {"target_count": 3}, {"minimum_count": 1}, {"required_strong_factors": 3}, {"minimum_discount_pct": 0}, {"reason": "fill_the_quota"},
    {"evaluated_at": "2020-01-01T00:00:00+00:00"},
])
def test_published_policy_metadata_is_validated_and_cannot_misrepresent_a_short_edition(change):
    prepared = publisher.prepare_publication(edition())
    prepared["publication_policy"].update(change)
    with pytest.raises(ValueError):
        dp.validate_snapshot(prepared)


def test_policy_projection_drops_unrelated_private_fields():
    prepared = publisher.prepare_publication(edition())
    prepared["publication_policy"]["seller_phone"] = "PRIVATE-CONTACT"
    assert "PRIVATE" not in json.dumps(dp.validate_snapshot(prepared))


def configure_cli(monkeypatch, tmp_path, snapshot):
    public = tmp_path / "daily-picks.json"
    public.write_bytes(b"previous verified edition")
    @contextmanager
    def lock(path):
        yield
    async def generate(args, now):
        return {"snapshot": snapshot, "input_count": 123, "photo_heads": len(snapshot["items"]), "source_gets": 0}
    monkeypatch.setattr(publisher, "snapshot_path", lambda: public)
    monkeypatch.setattr(publisher, "publisher_lock", lock)
    monkeypatch.setattr(publisher, "generate", generate)
    return public


def test_cli_explicit_policy_atomically_publishes_short_edition_and_journals_policy(tmp_path, monkeypatch, capsys):
    public = configure_cli(monkeypatch, tmp_path, edition())
    monkeypatch.setattr(publisher.sys, "argv", ["publish_daily_picks.py", "--quality-policy", "discounted_four_factors"])
    assert publisher.main() == 0
    saved = dp.load_daily_picks(public)
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "published" and report["count"] == 10
    assert saved["publication_policy"] == report["publication_policy"]
    assert report["quality_policy"] == "discounted_four_factors"


def test_cli_default_retains_previous_edition_when_only_nine_pass(tmp_path, monkeypatch, capsys):
    public = configure_cli(monkeypatch, tmp_path, edition(9))
    monkeypatch.setattr(publisher.sys, "argv", ["publish_daily_picks.py"])
    assert publisher.main() == 1
    assert public.read_bytes() == b"previous verified edition"
    assert json.loads(capsys.readouterr().err)["previous_snapshot_retained"] is True


def test_cli_forbids_ambiguous_combination_with_legacy_partial_flag(monkeypatch):
    monkeypatch.setattr(publisher.sys, "argv", ["publish_daily_picks.py", "--allow-partial"])
    with pytest.raises(SystemExit) as error:
        publisher.main()
    assert error.value.code == 2


def test_legacy_three_factor_policy_is_never_accepted_for_skip_or_publication():
    snapshot = edition()
    snapshot["publication_policy"] = {"name": "three_factors", "published_count": 10, "explicit": True}
    assert not publisher.publication_satisfies_policy(snapshot)
    assert not publisher.publication_satisfies_policy(snapshot, "three_factors")
    with pytest.raises(ValueError):
        publisher.prepare_publication(snapshot, "three_factors")


@pytest.mark.parametrize("count", [1, 9])
def test_new_minimum_cannot_be_bypassed_by_partial_or_default_publication(count):
    snapshot = edition(count)
    with pytest.raises(RuntimeError):
        publisher.prepare_publication(snapshot)
    with pytest.raises(ValueError):
        publisher.prepare_publication(snapshot, allow_partial=True)


def test_four_other_positive_attributes_do_not_replace_the_required_discount():
    snapshot = edition()
    item = snapshot["items"][0]
    item["price_comparison"].update(discount_pct=2, reference_price_per_m2=408_163)
    item["selection_facts"].update(is_owner=True, owner_source_verified=True)
    with pytest.raises(ValueError):
        publisher.prepare_publication(snapshot)


PHOTO_A = "https://krisha-photos.kcdn.online/webp/c7/c70532b0-9310-4f48-bc2d-e59a83f9f345/5-full.jpg"
PHOTO_B = PHOTO_A.replace("/5-full", "/6-full")
PHOTO_C = PHOTO_A.replace("/5-full", "/7-full")
PHOTO_D = PHOTO_A.replace("/5-full", "/8-full")


def photo_review_row(now):
    source, record = checked_measurement_row()
    source.update(candidate_photo_urls=[PHOTO_A, PHOTO_B, PHOTO_C, PHOTO_D], approved_photo_urls=[])
    record.update(checked_at=now.isoformat(), interior_photo_urls=[PHOTO_B],
                  photo_review={"method": "normal_browser_visual", "checked_at": now.isoformat()})
    return source, record


def test_human_reviewed_photo_must_be_fresh_source_linked_and_current_without_mutating_inputs():
    now = datetime.now(timezone.utc)
    source, record = photo_review_row(now)
    before = copy.deepcopy(source)
    reviewed = publisher.photo_evidence_rows([source], {source["id"]: record}, now)[0]
    assert reviewed["approved_photo_urls"] == [PHOTO_B]
    assert source == before
    assert publisher.photo_evidence_rows([source], None, now)[0]["approved_photo_urls"] == []
    preview = publisher.photo_evidence_rows([source], None, now, review_preview=True)[0]
    assert preview["approved_photo_urls"] == [PHOTO_A, PHOTO_B, PHOTO_C]


@pytest.mark.parametrize("change", [
    {"source_url": "https://krisha.kz/a/show/9999999999"}, {"id": "9999999999"}, {"price": 99_000_000},
    {"interior_photo_urls": [PHOTO_D.replace("/8-full", "/99-full")]},
    {"interior_photo_urls": ["https://example.com/photo.jpg"]},
    {"interior_photo_urls": [PHOTO_B + "?tracking=1"]},
    {"interior_photo_urls": [PHOTO_A, PHOTO_B, PHOTO_C, PHOTO_D]},
    {"interior_photo_urls": []}, {"interior_photo_urls": "photo"},
    {"photo_review": None}, {"photo_review": {"method": "automatic"}},
])
def test_untrusted_or_non_current_photo_proofs_are_rejected(change):
    now = datetime.now(timezone.utc)
    source, record = photo_review_row(now)
    record.update(change)
    with pytest.raises(ValueError):
        publisher.photo_evidence_rows([source], {source["id"]: record}, now)


@pytest.mark.parametrize("change", [{"photo_type": "exterior"}, {"photo_type": "layout"},
                                     {"checked_at": "2020-01-01T00:00:00+00:00"}])
def test_noninterior_or_differently_timed_visual_receipts_are_rejected(change):
    now = datetime.now(timezone.utc)
    source, record = photo_review_row(now)
    record["photo_review"].update(change)
    with pytest.raises(ValueError):
        publisher.photo_evidence_rows([source], {source["id"]: record}, now)


@pytest.mark.parametrize("offset", [timedelta(hours=-3), timedelta(seconds=1)])
def test_stale_or_future_source_and_photo_receipts_are_rejected(offset):
    now = datetime.now(timezone.utc)
    source, record = photo_review_row(now + offset)
    with pytest.raises(ValueError):
        publisher.photo_evidence_rows([source], {source["id"]: record}, now)


def test_existing_approved_photos_stay_usable_and_total_photo_count_is_bounded():
    now = datetime.now(timezone.utc)
    source, record = photo_review_row(now)
    source["approved_photo_urls"] = [PHOTO_A]
    record["interior_photo_urls"] = [PHOTO_B, PHOTO_C, PHOTO_D]
    reviewed = publisher.photo_evidence_rows([source], {source["id"]: record}, now)[0]
    assert reviewed["approved_photo_urls"] == [PHOTO_A, PHOTO_B, PHOTO_C]


@pytest.mark.asyncio
async def test_private_export_contains_full_safe_current_gallery_without_expanding_public_preview(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    rows = edition_rows(now=now)
    for source in rows:
        source.update(candidate_photo_urls=[PHOTO_A, PHOTO_B, PHOTO_C, PHOTO_D, PHOTO_A, "https://example.com/not-a-photo"],
                      approved_photo_urls=[])
    async def read(*args):
        return rows
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused:unused@127.0.0.1:1/private_gallery_test")
    monkeypatch.setattr("dotenv.load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(publisher, "read_candidates", read)
    monkeypatch.setattr(publisher, "load_daily_picks", lambda *args: None)
    exported = await publisher.export_candidate_pool(str(tmp_path / "private-candidates.json"), now)
    assert exported["kind"] == "unverified_review_candidates" and len(exported["items"]) == 10
    assert all(item["review_photo_urls"] == [PHOTO_A, PHOTO_B, PHOTO_C, PHOTO_D] for item in exported["items"])
    assert all(len(item["photo_urls"]) <= 3 for item in exported["items"])
    assert all(source["approved_photo_urls"] == [] for source in rows)


@pytest.mark.asyncio
async def test_raw_photos_never_reach_real_generation_without_each_fresh_visual_receipt(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    rows = edition_rows(now=now)
    for source in rows:
        source.update(candidate_photo_urls=[PHOTO_A], approved_photo_urls=[])
    records = [{"id": source["id"], "source_url": source["url"], "status": "active", "sale_astana": True,
                "checked_at": now.isoformat(),
                **{field: source[field] for field in ("price", "rooms", "area", "floor", "floors_total")}}
               for source in rows[:10]]
    path = tmp_path / "source-checks.json"
    def save():
        path.write_text(json.dumps({"method": "normal_browser", "items": records}), encoding="utf-8")
    async def read(*args):
        return rows
    async def photos(snapshot, deadline):
        return snapshot, len(snapshot["items"])
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused:unused@127.0.0.1:1/daily_photo_review_test")
    monkeypatch.setattr("dotenv.load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(publisher, "read_candidates", read)
    monkeypatch.setattr(publisher, "verify_photos", photos)
    save()
    args = SimpleNamespace(skip_photo_check=False, source_checks=str(path))
    unreviewed = await publisher.generate(args, now)
    assert unreviewed["snapshot"]["items"] == []
    for record in records:
        record.update(interior_photo_urls=[PHOTO_A],
                      photo_review={"method": "normal_browser_visual", "checked_at": record["checked_at"]})
    save()
    reviewed = await publisher.generate(args, now)
    prepared = publisher.prepare_publication(reviewed["snapshot"])
    assert len(prepared["items"]) == 10 and reviewed["source_gets"] == 0
    assert all(item["photo_urls"] == [PHOTO_A] for item in prepared["items"])
    assert all("photo_review" not in item for item in prepared["items"])
    assert all("review_photo_urls" not in item for item in prepared["items"])
    reviewed["snapshot"]["items"][0]["review_photo_urls"] = [PHOTO_A, PHOTO_B, PHOTO_C, PHOTO_D]
    assert "review_photo_urls" not in dp.validate_snapshot(reviewed["snapshot"])["items"][0]
    assert all(source["approved_photo_urls"] == [] for source in rows)


def checked_measurement_row():
    row = edition_rows()[0]
    row.update(is_owner=False, kitchen_area=None, ceiling_height=None)
    record = {"id": row["id"], "source_url": row["url"], "status": "active", "sale_astana": True,
              **{field: row[field] for field in ("price", "rooms", "area", "floor", "floors_total")}}
    return row, record


def test_matching_browser_measurements_enrich_the_edition_independently_of_owner_without_mutation():
    row, record = checked_measurement_row()
    before = copy.deepcopy(row)
    record.update(kitchen_area=14.4, ceiling_height=3, description="PRIVATE", seller_phone="PRIVATE")
    result = publisher.owner_evidence_rows([row], {row["id"]: record})[0]
    assert result["kitchen_area"] == 14.4 and result["ceiling_height"] == 3
    assert result["owner_source_verified"] is False
    assert "description" not in result and "seller_phone" not in result
    assert row == before


@pytest.mark.parametrize("field,value", [("kitchen_area", True), ("kitchen_area", "14.4"),
    ("kitchen_area", float("nan")), ("kitchen_area", float("inf")), ("kitchen_area", 2),
    ("kitchen_area", 36.2), ("ceiling_height", False), ("ceiling_height", 1.9),
    ("ceiling_height", 5.1), ("ceiling_height", float("nan"))])
def test_browser_measurements_reject_wrong_types_nonfinite_and_out_of_bounds(field, value):
    row, record = checked_measurement_row()
    record[field] = value
    with pytest.raises(ValueError):
        publisher.owner_evidence_rows([row], {row["id"]: record})


def test_missing_null_or_mismatched_source_measurements_never_erase_or_replace_recorded_facts():
    row, record = checked_measurement_row()
    row.update(kitchen_area=12, ceiling_height=2.8)
    record.update(kitchen_area=None)
    result = publisher.owner_evidence_rows([row], {row["id"]: record})[0]
    assert result["kitchen_area"] == 12 and result["ceiling_height"] == 2.8
    record.update(price=row["price"] + 1, kitchen_area=14.4, ceiling_height=3)
    mismatch = publisher.owner_evidence_rows([row], {row["id"]: record})[0]
    assert mismatch["kitchen_area"] == 12 and mismatch["ceiling_height"] == 2.8


@pytest.mark.parametrize("change", [{"price": 31_000_000}, {"source_checked_at": "2020-01-01T00:00:00+00:00"},
                                      {"source_status": "archived"}])
def test_private_discovery_never_reuses_changed_or_old_source_measurements(change):
    now = datetime.now(timezone.utc)
    source, record = checked_measurement_row()
    previous = {**record, "source_status": "active", "source_checked_at": now.isoformat(),
                "selection_facts": {"kitchen_area": 12, "ceiling_height": 3}, **change}
    result = publisher.preview_with_recent_measurements([source], {"items": [previous]}, now)[0]
    assert result["kitchen_area"] is None and result["ceiling_height"] is None


def test_private_discovery_fills_only_missing_values_from_a_recent_exactly_matched_edition():
    now = datetime.now(timezone.utc)
    source, record = checked_measurement_row()
    source["ceiling_height"] = 2.7
    previous = {**record, "source_status": "active", "source_checked_at": now.isoformat(),
                "selection_facts": {"kitchen_area": 12, "ceiling_height": 3}}
    result = publisher.preview_with_recent_measurements([source], {"items": [previous]}, now)[0]
    assert result["kitchen_area"] == 12
    assert result["ceiling_height"] == 2.7
    assert source["kitchen_area"] is None


@pytest.mark.asyncio
async def test_preview_discovers_previously_checked_kitchen_but_generate_requires_new_browser_measurement(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    target = row(0, price=24_000_000, floor=5, floors_total=12, kitchen_area=None, ceiling_height=3,
                 is_owner=False, last_seen=now)
    peers = [row(index, price=30_000_000, floor=1, ceiling_height=None, kitchen_area=None,
                 is_owner=False, last_seen=now) for index in range(1, 8)]
    rows = [target, *peers]
    previous = dp.build_daily_picks([{**target, "kitchen_area": 12}, *peers], now)
    assert len(previous["items"]) == 1
    previous["items"][0].update(source_status="active", source_checked_at=now.isoformat())
    loaded = []
    def load_previous(*args):
        loaded.append(True)
        return dp.validate_snapshot(previous)
    async def read(*args):
        return rows
    async def photos(snapshot, deadline):
        return snapshot, len(snapshot["items"])
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused:unused@127.0.0.1:1/daily_discovery_test")
    monkeypatch.setattr("dotenv.load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(publisher, "read_candidates", read)
    monkeypatch.setattr(publisher, "verify_photos", photos)
    monkeypatch.setattr(publisher, "load_daily_picks", load_previous)
    exported = await publisher.export_candidate_pool(str(tmp_path / "unverified-candidates.json"), now)
    assert exported["kind"] == "unverified_review_candidates"
    assert exported["items"][0]["selection_facts"]["kitchen_area"] == 12
    assert target["kitchen_area"] is None
    record = {"id": target["id"], "source_url": target["url"], "status": "active", "sale_astana": True,
              "is_owner": False, "checked_at": now.isoformat(),
              **{field: target[field] for field in ("price", "rooms", "area", "floor", "floors_total")}}
    path = tmp_path / "new-browser-confirmation.json"
    path.write_text(json.dumps({"method": "normal_browser", "items": [record]}), encoding="utf-8")
    generated = await publisher.generate(SimpleNamespace(skip_photo_check=False, source_checks=str(path)), now)
    assert generated["snapshot"]["items"] == []
    assert loaded == [True]  # Only the private discovery step reads the old edition.


@pytest.mark.asyncio
async def test_source_facts_survive_real_generation_and_its_final_rebuild(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    rows = edition_rows(now=now)
    confirmed = dp.build_daily_picks(rows, now)
    pledge_id = confirmed["items"][0]["id"]
    records = [{"id": item["id"], "source_url": item["source_url"], "status": "active",
                "checked_at": now.isoformat(), "sale_astana": True, "price": item["price"],
                "rooms": item["rooms"], "area": item["area"], "floor": item["floor"],
                "floors_total": item["floors_total"], "is_pledged": item["id"] == pledge_id}
               for item in confirmed["items"]]
    for source_row in rows:
        if source_row["id"] == pledge_id:
            source_row.update(kitchen_area=None, ceiling_height=None)
    for record in records:
        if record["id"] == pledge_id:
            record.update(kitchen_area=14.4, ceiling_height=3)
    path = tmp_path / "source-checks.json"
    path.write_text(json.dumps({"method": "normal_browser", "items": records}), encoding="utf-8")
    async def read(*args):
        return rows
    async def photos(snapshot, deadline):
        return snapshot, len(snapshot["items"])
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused:unused@127.0.0.1:1/daily_quality_test")
    monkeypatch.setattr("dotenv.load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(publisher, "read_candidates", read)
    monkeypatch.setattr(publisher, "verify_photos", photos)
    result = await publisher.generate(SimpleNamespace(skip_photo_check=False, source_checks=str(path)), now)
    assert result["source_gets"] == 0
    prepared = publisher.prepare_publication(result["snapshot"])
    pledged = next(item for item in prepared["items"] if item["id"] == pledge_id)
    assert pledged["source_facts"] == {"is_pledged": True}
    assert pledged["selection_facts"]["kitchen_area"] == 14.4
    assert pledged["selection_facts"]["ceiling_height"] == 3
    assert any("Кухня 14,4" in reason for reason in pledged["reasons"])
    assert any("потолки 3" in reason for reason in pledged["reasons"])
    assert all(item["source_facts"] == {} for item in prepared["items"] if item["id"] != pledge_id)


@pytest.mark.asyncio
async def test_browser_pipeline_can_use_only_explicit_policy_for_a_short_verified_edition(tmp_path, monkeypatch):
    snapshot = edition()
    @contextmanager
    def lock(path):
        yield
    async def export(path, now):
        return {"kind": "unverified_review_candidates", "input_count": 123, "items": snapshot["items"]}
    async def check(*args, **kwargs):
        return {"items": [{"status": "active"} for _ in snapshot["items"]]}
    async def generate(*args):
        return {"snapshot": snapshot, "photo_heads": 10, "source_gets": 0}
    monkeypatch.setattr(publisher, "export_candidate_pool", export)
    monkeypatch.setattr(publisher, "generate", generate)
    monkeypatch.setattr(publisher, "publisher_lock", lock)
    monkeypatch.setattr(browser, "check_sources", check)
    args = SimpleNamespace(budget_seconds=60, check_limit=90, profile_path=tmp_path / "profile",
                           headed=False, dry_run=False, force=True, quality_policy="discounted_four_factors")
    public = tmp_path / "public.json"
    result = await browser.pipeline(args, tmp_path, {}, public)
    assert result["published"] is True and result["count"] == 10
    assert result["publication_policy"] == dp.load_daily_picks(public)["publication_policy"]
