"""Cached photo labels cannot replace this edition's human interior review."""
import copy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from scripts import publish_daily_picks as publisher
from tests.test_daily_picks_quality_policy import PHOTO_A, PHOTO_B, edition_rows, photo_review_row


@pytest.fixture(autouse=True)
def no_live_io(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://placeholder:placeholder@127.0.0.1:1/required_visual_test")
    async def deny(*args, **kwargs):
        raise AssertionError("No live database, source or image request in visual receipt tests")
    for name in ("connect", "create_pool"):
        monkeypatch.setattr("asyncpg." + name, deny)
    monkeypatch.setattr("bot.db.pg.init_pool", deny)
    monkeypatch.setattr(publisher, "read_candidates", deny)
    monkeypatch.setattr(publisher, "verify_sources", deny)
    monkeypatch.setattr(publisher, "verify_photos", deny)
    monkeypatch.setattr("httpx.AsyncClient.request", deny)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *args, **kwargs: False)


@pytest.mark.parametrize("proof", ["no_record", "only_current_source", "null_visual_fields"])
def test_cached_approved_current_interior_needs_this_visits_visual_receipt(proof):
    now = datetime.now(timezone.utc)
    source, record = photo_review_row(now)
    source["approved_photo_urls"] = [PHOTO_A]
    if proof != "no_record":
        record.pop("interior_photo_urls")
        record.pop("photo_review")
        if proof == "null_visual_fields":
            record.update(interior_photo_urls=None, photo_review=None)
    checks = None if proof == "no_record" else {source["id"]: record}
    before = copy.deepcopy((source, checks))
    rows = publisher.photo_evidence_rows([source], checks, now)
    assert len(rows) == 1 and rows[0]["id"] == source["id"]  # Still a comparable.
    assert rows[0]["approved_photo_urls"] == []
    assert (source, checks) == before
    assert publisher.photo_evidence_rows([source], None, now, review_preview=True)[0]["approved_photo_urls"]


def test_current_receipt_approves_only_reviewed_frame_and_does_not_retime_or_mutate():
    now = datetime.now(timezone.utc)
    source, record = photo_review_row(now)
    source["approved_photo_urls"] = [PHOTO_A]
    before = copy.deepcopy((source, record))
    result = publisher.photo_evidence_rows([source], {source["id"]: record}, now)[0]
    assert result["approved_photo_urls"] == [PHOTO_B]
    assert (source, record) == before


@pytest.mark.parametrize("field,value", [
    ("price", 99_000_000), ("rooms", 3), ("area", 99), ("floor", 7), ("floors_total", 19),
    ("source_url", "https://krisha.kz/a/show/9999999999"), ("status", "unknown"), ("sale_astana", False),
])
def test_old_fingerprint_never_rescues_visual_proof_for_changed_source_facts(field, value):
    now = datetime.now(timezone.utc)
    source, record = photo_review_row(now)
    source["approved_photo_urls"] = [PHOTO_A]
    record[field] = value
    with pytest.raises(ValueError):
        publisher.photo_evidence_rows([source], {source["id"]: record}, now)


@pytest.mark.parametrize("offset", [timedelta(hours=-2, microseconds=-1), timedelta(microseconds=1)])
def test_old_fingerprint_does_not_restore_stale_or_future_visual_proof(offset):
    now = datetime.now(timezone.utc)
    source, record = photo_review_row(now + offset)
    source["approved_photo_urls"] = [PHOTO_A]
    with pytest.raises(ValueError):
        publisher.photo_evidence_rows([source], {source["id"]: record}, now)


def test_source_and_visual_receipts_must_keep_the_exact_same_observed_timestamp():
    now = datetime.now(timezone.utc)
    source, record = photo_review_row(now)
    source["approved_photo_urls"] = [PHOTO_A]
    record["photo_review"]["checked_at"] = (now-timedelta(microseconds=1)).isoformat()
    with pytest.raises(ValueError):
        publisher.photo_evidence_rows([source], {source["id"]: record}, now)


@pytest.mark.asyncio
async def test_nine_visual_receipts_plus_cached_tenth_cannot_publish_or_change_reference_population(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    rows = edition_rows(now=now)
    for source in rows:
        source.update(candidate_photo_urls=[PHOTO_A], approved_photo_urls=[PHOTO_A])
    before = copy.deepcopy(rows)
    records = [{"id": source["id"], "source_url": source["url"], "status": "active", "sale_astana": True,
                "checked_at": now.isoformat(), "source_complex_name": "Тестовый ЖК",
                **{field: source[field] for field in ("price", "rooms", "area", "floor", "floors_total")}}
               for source in rows[:10]]
    for record in records[:9]:
        record.update(interior_photo_urls=[PHOTO_A], photo_review={"method": "normal_browser_visual",
                      "photo_type": "interior", "checked_at": record["checked_at"]})
    checks_path = tmp_path / "private-source-checks.json"
    checks_path.write_text(json.dumps({"method": "normal_browser", "items": records}), encoding="utf-8")
    public = tmp_path / "daily-picks.json"
    public.write_bytes(b"previous verified edition")
    old_bytes, old_mtime = public.read_bytes(), public.stat().st_mtime_ns
    async def read(*args):
        return rows
    checked_ids = set()
    async def photos(snapshot, deadline):
        checked_ids.update(item["id"] for item in snapshot["items"])
        assert all(item["photo_urls"] == [PHOTO_A] for item in snapshot["items"])
        return snapshot, len(snapshot["items"])
    monkeypatch.setattr(publisher, "read_candidates", read)
    monkeypatch.setattr(publisher, "verify_photos", photos)
    generated = await publisher.generate(SimpleNamespace(skip_photo_check=False, source_checks=str(checks_path)), now)
    assert len(generated["snapshot"]["items"]) == 9
    assert checked_ids == {record["id"] for record in records[:9]}
    assert all(item["price_comparison"]["comparable_count"] == len(rows)-1 for item in generated["snapshot"]["items"])
    with pytest.raises(RuntimeError, match="Only 9 qualifying cards"):
        prepared = publisher.prepare_publication(generated["snapshot"], "discounted_four_factors")
        publisher.publish_snapshot(prepared, public)
    assert public.read_bytes() == old_bytes and public.stat().st_mtime_ns == old_mtime
    assert rows == before
    assert json.loads(checks_path.read_text(encoding="utf-8"))["items"] == records
