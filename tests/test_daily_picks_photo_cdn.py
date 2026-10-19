"""Observed gallery derivatives retain asset identity and visual proof gates."""
import copy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import httpx
import pytest

from bot.core import daily_picks as dp
from scripts import publish_daily_picks as publisher
from tests.test_daily_picks_quality_policy import edition_rows, photo_review_row


OLD = "https://krisha-photos.kcdn.online/webp/97/975ab662-be68-44d9-9ab1-9af315e70c7a/23-full.jpg"
# Exact gallery DOM URL personally inspected by the parent browser session.
OBSERVED = "https://astps-photos-kr.kcdn.kz/webp/97/975ab662-be68-44d9-9ab1-9af315e70c7a/23-full.webp"
ALAPS_OLD = "https://krisha-photos.kcdn.online/webp/6a/6a1e2d8e-e8f2-4a2e-8b80-cf28b6aea53c/1-full.jpg"
# Literal URL inspected by the parent in listing 1015074964 on 2026-10-17.
ALAPS_OBSERVED = "https://alaps-photos-kr.kcdn.kz/webp/6a/6a1e2d8e-e8f2-4a2e-8b80-cf28b6aea53c/1-full.webp"
KZALA2_OLD = "https://krisha-photos.kcdn.online/webp/61/611a773c-1099-47fb-845c-10ff530c773c/1-full.jpg"
# Literal gallery URL inspected by the parent in listing 1013704636 on 2026-10-19.
KZALA2_OBSERVED = "https://kzala2-photos-kr.kcdn.kz/webp/61/611a773c-1099-47fb-845c-10ff530c773c/1-full.webp"
OBSERVED_CDNS = [(OLD, OBSERVED), (ALAPS_OLD, ALAPS_OBSERVED), (KZALA2_OLD, KZALA2_OBSERVED)]


@pytest.fixture(autouse=True)
def no_live_database(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused:unused@127.0.0.1:1/photo_cdn_test")
    async def reject(*args, **kwargs):
        raise AssertionError("Photo CDN tests must not access a database")
    monkeypatch.setattr("asyncpg.connect", reject)
    monkeypatch.setattr("asyncpg.create_pool", reject)
    monkeypatch.setattr("bot.db.pg.init_pool", reject)


def checked_photo(now, old=OLD, observed=OBSERVED):
    source, check = photo_review_row(now)
    source.update(candidate_photo_urls=[old], approved_photo_urls=[])
    check.update(interior_photo_urls=[observed])
    check["photo_review"]["photo_type"] = "interior"
    return source, check


def test_exact_observed_url_is_allowlisted_without_rewriting_and_identity_matches():
    assert dp.safe_photo_url(OBSERVED) == OBSERVED
    assert dp.safe_photo_url(OLD) == OLD
    assert dp.photo_asset_key(OBSERVED) == dp.photo_asset_key(OLD) == ("975ab662-be68-44d9-9ab1-9af315e70c7a", "23")
    assert dp.photo_asset_key(OBSERVED.replace("23-full", "24-full")) != dp.photo_asset_key(OLD)
    assert dp.photo_asset_key(OBSERVED.replace("975ab662", "975ab663")) != dp.photo_asset_key(OLD)
    assert dp.photo_asset_key("https://example.com/photo.jpg") is None


def test_alaps_literal_url_keeps_asset_identity_and_requires_its_own_fresh_receipt():
    assert dp.safe_photo_url(ALAPS_OBSERVED) == ALAPS_OBSERVED
    assert dp.photo_asset_key(ALAPS_OBSERVED) == dp.photo_asset_key(ALAPS_OLD) == ("6a1e2d8e-e8f2-4a2e-8b80-cf28b6aea53c", "1")
    now = datetime.now(timezone.utc)
    source, check = checked_photo(now)
    source.update(candidate_photo_urls=[ALAPS_OLD], approved_photo_urls=[])
    check["interior_photo_urls"] = [ALAPS_OBSERVED]
    assert publisher.photo_evidence_rows([source], None, now)[0]["approved_photo_urls"] == []
    assert publisher.photo_evidence_rows([source], {source["id"]: check}, now)[0]["approved_photo_urls"] == [ALAPS_OBSERVED]
    check["interior_photo_urls"] = [ALAPS_OBSERVED.replace("/1-full", "/2-full")]
    with pytest.raises(ValueError, match="current source listing"):
        publisher.photo_evidence_rows([source], {source["id"]: check}, now)


def test_kzala2_literal_url_matches_only_its_stored_uuid_and_frame_without_rewriting():
    assert dp.safe_photo_url(KZALA2_OBSERVED) == KZALA2_OBSERVED
    assert dp.photo_asset_key(KZALA2_OBSERVED) == dp.photo_asset_key(KZALA2_OLD) == ("611a773c-1099-47fb-845c-10ff530c773c", "1")
    now = datetime.now(timezone.utc)
    source, check = checked_photo(now, KZALA2_OLD, KZALA2_OBSERVED)
    older = [KZALA2_OLD.replace("1-full", f"{index}-full") for index in (2, 3, 4)]
    source.update(candidate_photo_urls=[*older, KZALA2_OLD], approved_photo_urls=older)
    before = copy.deepcopy((source, check))
    result = publisher.photo_evidence_rows([source], {source["id"]: check}, now)[0]
    assert result["approved_photo_urls"] == [KZALA2_OBSERVED]
    assert (source, check) == before
    for wrong in (KZALA2_OBSERVED.replace("/1-full", "/5-full"),
                  KZALA2_OBSERVED.replace("611a773c", "611a773d")):
        check["interior_photo_urls"] = [wrong]
        with pytest.raises(ValueError, match="current source listing"):
            publisher.photo_evidence_rows([source], {source["id"]: check}, now)


@pytest.mark.parametrize("url", [
    KZALA2_OBSERVED.replace("https://", "http://"),
    KZALA2_OBSERVED.replace("kzala2-photos-kr.kcdn.kz", "kzala2-photos-kr.kcdn.kz.evil.test"),
    KZALA2_OBSERVED.replace("kzala2-photos-kr.kcdn.kz", "kzala3-photos-kr.kcdn.kz"),
    KZALA2_OBSERVED.replace("https://", "https://user:pass@"),
    KZALA2_OBSERVED.replace("https://", "https://@"),
    KZALA2_OBSERVED.replace(".kz/", ".kz:443/"),
    KZALA2_OBSERVED.replace(".kz/", ".kz:invalid/"),
    KZALA2_OBSERVED + "?", KZALA2_OBSERVED + "?token=private",
    KZALA2_OBSERVED + "#", KZALA2_OBSERVED + "#image",
    KZALA2_OBSERVED + "\n", KZALA2_OBSERVED.replace("/1-full", "/-1-full"),
    KZALA2_OBSERVED.replace("/1-full.webp", "/1-full.png"),
    KZALA2_OBSERVED.replace("/1-full.webp", "/1-750x470.webp"),
    KZALA2_OBSERVED.replace("611a773c-1099-47fb-845c-10ff530c773c", "611a773c1099-47fb-845c-10ff530c773cc"),
    KZALA2_OBSERVED.replace("/webp/61/", "/webp/../61/"),
])
def test_kzala2_sale_and_rental_do_not_allow_lookalikes_or_unsafe_url_shapes(url):
    from bot.core.rental_parser import _rental_card_photo_url
    assert dp.safe_photo_url(url) is None
    assert dp.photo_asset_key(url) is None
    assert _rental_card_photo_url(url) is None


@pytest.mark.parametrize("url", [
    ALAPS_OBSERVED.replace("https://", "http://"),
    ALAPS_OBSERVED.replace("alaps-photos-kr.kcdn.kz", "alaps-photos-kr.kcdn.kz.evil.test"),
    ALAPS_OBSERVED.replace("alaps-photos-kr.kcdn.kz", "alaps2-photos-kr.kcdn.kz"),
    ALAPS_OBSERVED.replace("https://", "https://user:pass@"),
    ALAPS_OBSERVED.replace("https://", "https://@"),
    ALAPS_OBSERVED.replace(".kz/", ".kz:443/"),
    ALAPS_OBSERVED.replace(".kz/", ".kz:invalid/"),
    ALAPS_OBSERVED + "?", ALAPS_OBSERVED + "?token=private", ALAPS_OBSERVED + "#", ALAPS_OBSERVED + "#image",
    ALAPS_OBSERVED + "\n", ALAPS_OBSERVED.replace("/1-full", "/-1-full"),
    ALAPS_OBSERVED.replace("/1-full.webp", "/1-full.png"),
    ALAPS_OBSERVED.replace("/1-full.webp", "/1-750x470.webp"),
    ALAPS_OBSERVED.replace("6a1e2d8e-e8f2-4a2e-8b80-cf28b6aea53c", "6a1e2d8ee8f2-4a2e-8b80-cf28b6aea53cc"),
    ALAPS_OBSERVED.replace("/webp/6a/", "/webp/../6a/"),
])
def test_alaps_host_variants_and_unsafe_urls_stay_rejected(url):
    assert dp.safe_photo_url(url) is None
    assert dp.photo_asset_key(url) is None


@pytest.mark.parametrize("url", [
    OBSERVED.replace("https://", "http://"),
    OBSERVED.replace("astps-photos-kr.kcdn.kz", "astps-photos-kr.kcdn.kz.evil.test"),
    OBSERVED.replace("astps-photos-kr.kcdn.kz", "other-photos-kr.kcdn.kz"),
    OBSERVED.replace("https://", "https://user@"),
    OBSERVED.replace("https://", "https://@"),
    OBSERVED.replace(".kz/", ".kz:443/"), OBSERVED.replace(".kz/", ".kz:invalid/"),
    OBSERVED + "?", OBSERVED + "?size=full", OBSERVED + "#", OBSERVED + "#image",
    OBSERVED + "\n", OBSERVED.replace("/23-", "/2\n3-"),
    OBSERVED.replace("975ab662-be68-44d9-9ab1-9af315e70c7a", "975ab662be68-44d9-9ab1-9af315e70c7aa"),
    OBSERVED.replace("/23-full.webp", "/-23-full.webp"),
    OBSERVED.replace("/23-full.webp", "/23-full.png"),
    OBSERVED.replace("/23-full.webp", "/23-750x470.webp"),
    OBSERVED.replace("/webp/97/", "/webp/../97/"),
])
def test_unobserved_hosts_and_unsafe_url_shapes_fail_closed(url):
    assert dp.safe_photo_url(url) is None
    assert dp.photo_asset_key(url) is None


def test_fresh_matching_visual_receipt_keeps_the_actual_observed_derivative_without_input_mutation():
    now = datetime.now(timezone.utc)
    source, check = checked_photo(now)
    before = copy.deepcopy((source, check))
    result = publisher.photo_evidence_rows([source], {source["id"]: check}, now)[0]
    assert result["approved_photo_urls"] == [OBSERVED]
    assert (source, check) == before
    assert publisher.photo_evidence_rows([source], None, now)[0]["approved_photo_urls"] == []


def test_observed_derivative_replaces_the_same_old_approved_asset_and_is_not_duplicated():
    now = datetime.now(timezone.utc)
    source, check = checked_photo(now)
    source["approved_photo_urls"] = [OLD]
    result = publisher.photo_evidence_rows([source], {source["id"]: check}, now)[0]
    assert result["approved_photo_urls"] == [OBSERVED]
    assert OLD not in result["approved_photo_urls"]


def test_three_older_approved_frames_cannot_displace_todays_reviewed_interior():
    now = datetime.now(timezone.utc)
    source, check = checked_photo(now)
    older = [OLD.replace("23-full", f"{index}-full") for index in (23, 24, 25)]
    newest = OBSERVED.replace("23-full", "26-full")
    source.update(candidate_photo_urls=[*older, OLD.replace("23-full", "26-full")],
                  approved_photo_urls=older)
    check["interior_photo_urls"] = [newest]
    result = publisher.photo_evidence_rows([source], {source["id"]: check}, now)[0]
    assert result["approved_photo_urls"] == [newest]
    assert source["approved_photo_urls"] == older


@pytest.mark.parametrize("field,value", [
    ("source_url", "https://krisha.kz/a/show/9999999999"), ("id", "9999999999"),
    ("price", 99_000_000), ("rooms", 6), ("area", 99.0), ("floor", 40), ("floors_total", 40),
    ("sale_astana", False), ("status", "unknown"),
])
@pytest.mark.parametrize("old,observed", OBSERVED_CDNS)
def test_new_cdn_does_not_bypass_fresh_offer_identity(field, value, old, observed):
    now = datetime.now(timezone.utc)
    source, check = checked_photo(now, old, observed)
    check[field] = value
    with pytest.raises(ValueError):
        publisher.photo_evidence_rows([source], {source["id"]: check}, now)


@pytest.mark.parametrize("url", [
    OBSERVED.replace("23-full", "24-full"),
    OBSERVED.replace("975ab662", "975ab663"),
    OBSERVED.replace("23-full", "023-full"),
])
def test_different_uuid_or_frame_never_borrows_another_gallery_asset(url):
    now = datetime.now(timezone.utc)
    source, check = checked_photo(now)
    check["interior_photo_urls"] = [url]
    with pytest.raises(ValueError, match="current source listing"):
        publisher.photo_evidence_rows([source], {source["id"]: check}, now)


@pytest.mark.parametrize("change", [
    {"method": "automatic"}, {"photo_type": "exterior"},
    {"checked_at": "2026-01-01T00:00:00+00:00"},
])
@pytest.mark.parametrize("old,observed", OBSERVED_CDNS)
def test_new_cdn_requires_the_same_interior_visual_receipt_contract(change, old, observed):
    now = datetime.now(timezone.utc)
    source, check = checked_photo(now, old, observed)
    check["photo_review"].update(change)
    with pytest.raises(ValueError):
        publisher.photo_evidence_rows([source], {source["id"]: check}, now)


@pytest.mark.parametrize("offset", [timedelta(hours=-3), timedelta(seconds=1)])
@pytest.mark.parametrize("old,observed", OBSERVED_CDNS)
def test_new_cdn_cannot_reuse_stale_or_future_visual_proof(offset, old, observed):
    now = datetime.now(timezone.utc)
    source, check = checked_photo(now + offset, old, observed)
    with pytest.raises(ValueError):
        publisher.photo_evidence_rows([source], {source["id"]: check}, now)


def test_duplicate_asset_derivatives_do_not_count_as_multiple_reviewed_photos():
    now = datetime.now(timezone.utc)
    source, check = checked_photo(now)
    check["interior_photo_urls"] = [OBSERVED, OLD]
    with pytest.raises(ValueError):
        publisher.photo_evidence_rows([source], {source["id"]: check}, now)


@pytest.mark.asyncio
@pytest.mark.parametrize("old,observed", OBSERVED_CDNS)
async def test_full_generation_preserves_observed_urls_quality_policy_and_head_only_checks(tmp_path, monkeypatch, old, observed):
    now = datetime.now(timezone.utc)
    rows = edition_rows(now=now)
    for source in rows:
        source.update(candidate_photo_urls=[old], approved_photo_urls=[])
    checks = [{"id": source["id"], "source_url": source["url"], "status": "active", "sale_astana": True,
               "checked_at": now.isoformat(), "interior_photo_urls": [observed],
               "source_complex_name": "Тестовый ЖК",
               "photo_review": {"method": "normal_browser_visual", "photo_type": "interior", "checked_at": now.isoformat()},
               **{field: source[field] for field in ("price", "rooms", "area", "floor", "floors_total")}}
              for source in rows[:10]]
    path = tmp_path / "checks.json"
    path.write_text(json.dumps({"method": "normal_browser", "items": checks}), encoding="utf-8")
    async def read(*args): return rows
    async def no_delay(*args): pass
    monkeypatch.setattr(publisher, "read_candidates", read)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **kw: None)
    monkeypatch.setattr(publisher.asyncio, "sleep", no_delay)
    requests = []
    def handler(request):
        requests.append((request.method, str(request.url)))
        assert str(request.url) == observed
        return httpx.Response(200, headers={"content-type": "image/webp"})
    original_client = httpx.AsyncClient
    def client(**kwargs):
        assert kwargs["follow_redirects"] is False
        return original_client(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", client)
    generated = await publisher.generate(SimpleNamespace(skip_photo_check=False, source_checks=str(path)), now)
    ready = publisher.prepare_publication(generated["snapshot"])
    assert len(ready["items"]) == 10
    assert generated["source_gets"] == 0
    assert requests == [("HEAD", observed)] * 10
    assert all(item["photo_urls"] == [observed] and len(item["reasons"]) == 4 for item in ready["items"])
    assert ready["publication_policy"]["required_strong_factors"] == 4
    assert ready["publication_policy"]["minimum_discount_pct"] == 3
    assert dp.validate_snapshot(ready) == ready
