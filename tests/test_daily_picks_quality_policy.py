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


def edition_rows(count=3, now=None):
    now = now or datetime.now(timezone.utc)
    return [row(index, price=24_000_000 if index < count else 30_000_000,
                floor=5 if index < count else 1, ceiling_height=3 if index < count else None,
                kitchen_area=None, last_seen=now)
            for index in range(count * 2 + 6)]


def edition(count=3):
    now = datetime.now(timezone.utc)
    snapshot = dp.build_daily_picks(edition_rows(count, now), now)
    assert len(snapshot["items"]) == count
    for item in snapshot["items"]:
        item.update(source_status="active", source_checked_at=now.isoformat())
    return snapshot


def test_explicit_three_factor_policy_keeps_every_check_and_records_actual_count():
    snapshot = edition()
    before = copy.deepcopy(snapshot)
    prepared = publisher.prepare_publication(snapshot, "three_factors")
    assert snapshot == before
    assert len(prepared["items"]) == 3
    assert all(len(item["reasons"]) == 3 and item["complex_identity"]["checked"] is True for item in prepared["items"])
    assert prepared["publication_policy"] == {
        "name": "three_factors", "target_count": 30, "published_count": 3,
        "required_strong_factors": 3, "reason": "quality_before_quota", "explicit": True,
        "evaluated_at": snapshot["generated_at"],
    }
    assert dp.validate_snapshot(prepared) == prepared
    assert publisher.publication_satisfies_policy(prepared, "three_factors")
    assert not publisher.publication_satisfies_policy(prepared, "daily_30")


def test_default_contract_still_requires_thirty_and_accepts_a_full_verified_edition():
    with pytest.raises(RuntimeError):
        publisher.prepare_publication(edition())
    full = edition(30)
    assert len(publisher.prepare_publication(full)["items"]) == 30
    assert publisher.publication_satisfies_policy(full)


@pytest.mark.parametrize("policy", ["daily_30", "three_factors"])
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
        publisher.prepare_publication(snapshot, "three_factors")


@pytest.mark.parametrize("change", [
    {"name": "allow_anything"}, {"explicit": False}, {"published_count": 30}, {"published_count": True},
    {"target_count": 3}, {"required_strong_factors": 2}, {"reason": "fill_the_quota"},
    {"evaluated_at": "2020-01-01T00:00:00+00:00"},
])
def test_published_policy_metadata_is_validated_and_cannot_misrepresent_a_short_edition(change):
    prepared = publisher.prepare_publication(edition(), "three_factors")
    prepared["publication_policy"].update(change)
    with pytest.raises(ValueError):
        dp.validate_snapshot(prepared)


def test_policy_projection_drops_unrelated_private_fields():
    prepared = publisher.prepare_publication(edition(), "three_factors")
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
    monkeypatch.setattr(publisher.sys, "argv", ["publish_daily_picks.py", "--quality-policy", "three_factors"])
    assert publisher.main() == 0
    saved = dp.load_daily_picks(public)
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "published" and report["count"] == 3
    assert saved["publication_policy"] == report["publication_policy"]
    assert report["quality_policy"] == "three_factors"


def test_cli_default_retains_previous_edition_when_only_three_pass(tmp_path, monkeypatch, capsys):
    public = configure_cli(monkeypatch, tmp_path, edition())
    monkeypatch.setattr(publisher.sys, "argv", ["publish_daily_picks.py"])
    assert publisher.main() == 1
    assert public.read_bytes() == b"previous verified edition"
    assert json.loads(capsys.readouterr().err)["previous_snapshot_retained"] is True


def test_cli_forbids_ambiguous_combination_with_legacy_partial_flag(monkeypatch):
    monkeypatch.setattr(publisher.sys, "argv", ["publish_daily_picks.py", "--quality-policy", "three_factors", "--allow-partial"])
    with pytest.raises(SystemExit) as error:
        publisher.main()
    assert error.value.code == 2


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
    prepared = publisher.prepare_publication(result["snapshot"], "three_factors")
    pledged = next(item for item in prepared["items"] if item["id"] == pledge_id)
    assert pledged["source_facts"] == {"is_pledged": True}
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
        return {"snapshot": snapshot, "photo_heads": 3, "source_gets": 0}
    monkeypatch.setattr(publisher, "export_candidate_pool", export)
    monkeypatch.setattr(publisher, "generate", generate)
    monkeypatch.setattr(publisher, "publisher_lock", lock)
    monkeypatch.setattr(browser, "check_sources", check)
    args = SimpleNamespace(budget_seconds=60, check_limit=90, profile_path=tmp_path / "profile",
                           headed=False, dry_run=False, force=True, quality_policy="three_factors")
    public = tmp_path / "public.json"
    result = await browser.pipeline(args, tmp_path, {}, public)
    assert result["published"] is True and result["count"] == 3
    assert result["publication_policy"] == dp.load_daily_picks(public)["publication_policy"]
