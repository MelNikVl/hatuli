"""Fresh-source metadata and fair bounded refresh, without source/DB access."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from bot.core.sale_detail_refresh import detail_due, detail_candidates, observed_detail_values

NOW = datetime(2026, 10, 19, 11, tzinfo=timezone.utc)


def row(identifier="100001", **updates):
    return dict(id=identifier, url=f"https://krisha.kz/a/show/{identifier}",
                title="2-комнатная, 50 м², 4/9 этаж", price=30000000, area=50,
                rooms=2, address="Example 1", district="Есиль", description="", **updates)


def stored(identifier="100001", **updates):
    value = dict(id=identifier, price=30000000, lat=51.1, complex_id=None,
                 complex_resolution=None, house_attribution=None,
                 found_at=NOW - timedelta(days=20),
                 source_details_checked_at=NOW - timedelta(hours=73),
                 source_details_attempted_at=None)
    value.update(updates)
    return value


def test_unchanged_price_and_existing_coordinates_still_expire_after_72_hours():
    assert detail_due(row(), stored(), now=NOW)
    assert not detail_due(row(), stored(source_details_checked_at=NOW-timedelta(hours=71)), now=NOW)


def test_legacy_details_flag_is_not_a_successful_source_date():
    assert detail_due(row(), stored(source_details_checked_at=None, details_fetched=True), now=NOW)


@pytest.mark.parametrize("date", [None, "bad", "2026-10-19", NOW+timedelta(hours=1)])
def test_unknown_naive_or_future_success_dates_are_not_fresh(date):
    assert detail_due(row(), stored(source_details_checked_at=date), now=NOW)


@pytest.mark.parametrize("field,value", [("price",31000000),("lat",None)])
def test_price_change_or_missing_coordinates_can_make_fresh_record_due(field,value):
    known = stored(source_details_checked_at=NOW-timedelta(hours=2))
    known[field] = value
    assert detail_due(row(), known, now=NOW)


def test_actual_attempt_retry_and_fifo_rotation_do_not_claim_success():
    old = stored(source_details_checked_at=NOW-timedelta(days=10))
    failed = stored("100002", source_details_checked_at=None,
                    source_details_attempted_at=NOW-timedelta(minutes=30))
    assert not detail_due(row("100002"), failed, now=NOW)
    failed["source_details_attempted_at"] = NOW-timedelta(hours=2)
    assert [r["id"] for r in detail_candidates([row("100002"),row()],
        {old["id"]:old,failed["id"]:failed}, now=NOW)] == ["100001","100002"]
    assert failed["source_details_checked_at"] is None


def test_source_patch_preserves_absence_and_does_not_promote_inferred_defaults():
    patch = observed_detail_values({"floor":7,"floors_total":None,"description":"",
        "is_new_build":False,"is_owner":False,"area_total":50,
        "address_full":"Example 1","listing_identity":{"rooms":2,"area":None}})
    assert patch == {"floor":7,"is_owner":False,"area":50,"rooms":2,"address":"Example 1"}
    assert observed_detail_values({"description":"","description_observed":True}) == {"description":""}


def test_complex_patch_preserves_official_raw_source_spelling_without_resolving_or_assigning_fk():
    patch = observed_detail_values({"source_detail_valid":True,"complex_source":"official_map_block",
        "complex_name":" Незнакомое написание ЖК ","complex_url":"https://krisha.kz/complex/show/55/",
        "complex_id":999,"canonical_name":"Catalog Guess"})
    assert patch == {"complex_name":"Незнакомое написание ЖК","complex_url":"https://krisha.kz/complex/show/55"}


@pytest.mark.parametrize("changes",[
    {"complex_source":"page_link"},{"complex_source":"text"},{"complex_source":None},
    {"source_detail_valid":False},{"is_archived":True},{"complex_name":" "},
    {"complex_url":None},{"complex_url":"https://evil.example/complex/show/55"},
    {"complex_url":"https://krisha.kz.evil.example/complex/show/55"},
    {"complex_url":"https://user:secret@krisha.kz/complex/show/55"},
    {"complex_url":"https://krisha.kz/complex/show/55?invented=1"},
])
def test_complex_patch_does_not_promote_missing_inferred_or_untrusted_source(changes):
    details = {"source_detail_valid":True,"complex_source":"official_map_block",
        "complex_name":"Исходное название","complex_url":"https://krisha.kz/complex/show/55"}
    details.update(changes)
    assert observed_detail_values(details) == {}


@pytest.fixture
def pipeline(monkeypatch):
    from bot.core import apartment_parser as parser, apartment_details as details
    from bot.db import pg, settings
    records = [row()]
    known = [stored()]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    monkeypatch.setattr(parser,"datetime",Clock)
    monkeypatch.setattr(parser,"parse_apartments_for_sale",AsyncMock(side_effect=lambda *a,**k:[dict(r) for r in records]))
    read = AsyncMock(side_effect=lambda *a,**k:[dict(r) for r in known])
    monkeypatch.setattr(pg,"fetch",read)
    monkeypatch.setattr(parser,"lookup_rental_estimate",AsyncMock(return_value=None))
    monkeypatch.setattr(parser,"get_comparables",AsyncMock(return_value=([],{})))
    monkeypatch.setattr(parser,"analyze_bargain",lambda *a,**k:{})
    monkeypatch.setattr(settings,"get_int",lambda key,default:1 if key=="DETAIL_FETCH_BATCH" else default)
    monkeypatch.setattr(parser.asyncio,"sleep",AsyncMock())
    monkeypatch.setattr(details,"detail_fetch_cooldown_remaining",lambda:0)
    monkeypatch.setattr(details,"REQUEST_COUNTS",{"detail":0})
    return parser,details,records,known,read


@pytest.mark.asyncio
@pytest.mark.parametrize("source", [{"source_detail_valid":False,"floor":99,"description":"foreign"},
                                    {"source_detail_valid":True,"is_archived":True,"floor":99},
                                    {"floor":99},{}])
async def test_truthy_unrecognized_or_archived_detail_never_overwrites_or_marks_fresh(pipeline,monkeypatch,source):
    parser,details,_,_,read = pipeline
    async def fetch(url):
        details.REQUEST_COUNTS["detail"] += 1
        return source
    monkeypatch.setattr(details,"fetch_apartment_details",fetch)
    result = (await parser.analyze_apartments())[0]
    assert result["floor"] == 4 and result["description"] == ""
    assert result.get("details_fetched") is not True
    assert "_source_details_checked_at" not in result
    assert datetime.fromisoformat(result["_source_details_attempted_at"]).tzinfo
    assert "source_details_checked_at" in read.await_args.args[0]


@pytest.mark.asyncio
async def test_only_recognized_same_id_success_sets_source_check_marker(pipeline,monkeypatch):
    parser,details,_,_,_ = pipeline
    async def fetch(url):
        details.REQUEST_COUNTS["detail"] += 1
        return {"source_detail_valid":True,"is_archived":False,"description":"fresh observed","floor":7}
    monkeypatch.setattr(details,"fetch_apartment_details",fetch)
    result = (await parser.analyze_apartments())[0]
    assert result["floor"] == 7 and result["description"] == "fresh observed" and result["details_fetched"] is True
    assert datetime.fromisoformat(result["_source_details_checked_at"]) >= datetime.fromisoformat(result["_source_details_attempted_at"])
    assert result["_source_detail_values"] == {"description":"fresh observed","floor":7}


@pytest.mark.asyncio
@pytest.mark.parametrize("actual_http",[True,False])
async def test_failed_detail_has_attempt_only_if_actual_http_started(pipeline,monkeypatch,actual_http):
    parser,details,_,_,_ = pipeline
    async def fetch(url):
        details.REQUEST_COUNTS["detail"] += int(actual_http)
        raise TimeoutError("mock transport")
    monkeypatch.setattr(details,"fetch_apartment_details",fetch)
    result = (await parser.analyze_apartments())[0]
    assert ("_source_details_attempted_at" in result) is actual_http
    assert "_source_details_checked_at" not in result and not result.get("details_fetched")


@pytest.mark.asyncio
async def test_local_return_without_actual_http_is_not_fresh_source_success(pipeline,monkeypatch):
    parser,details,_,_,_ = pipeline
    monkeypatch.setattr(details,"fetch_apartment_details",AsyncMock(return_value={"source_detail_valid":True,"floor":99}))
    result = (await parser.analyze_apartments())[0]
    assert result["floor"] == 4 and not result.get("details_fetched")
    assert "_source_details_checked_at" not in result and "_source_details_attempted_at" not in result


@pytest.mark.asyncio
async def test_cancelled_actual_request_exposes_attempt_only_for_caller_checkpoint(pipeline,monkeypatch):
    import asyncio
    parser,details,_,_,_ = pipeline
    async def fetch(url):
        details.REQUEST_COUNTS["detail"] += 1
        raise asyncio.CancelledError()
    monkeypatch.setattr(details,"fetch_apartment_details",fetch)
    stats = {"_detail_budget_remaining":1}
    with pytest.raises(asyncio.CancelledError) as caught:
        await parser.analyze_apartments(stats=stats)
    assert list(caught.value.sale_detail_attempts) == ["100001"]
    assert datetime.fromisoformat(caught.value.sale_detail_attempts["100001"]).tzinfo
    assert stats["_detail_budget_remaining"] == 0


@pytest.mark.asyncio
async def test_fifo_over_score_and_shared_remaining_budget_never_doubles_single_request_limit(pipeline,monkeypatch):
    parser,details,records,known,_ = pipeline
    records.append(row("100002"))
    known.append(stored("100002",source_details_checked_at=NOW-timedelta(days=30)))
    calls = []
    async def fetch(url):
        calls.append(url)
        details.REQUEST_COUNTS["detail"] += 1
        return {"source_detail_valid":True}
    monkeypatch.setattr(details,"fetch_apartment_details",fetch)
    stats = {"_detail_budget_remaining":1}
    await parser.analyze_apartments(stats=stats)
    assert calls == ["https://krisha.kz/a/show/100002"]
    assert stats["_detail_budget_remaining"] == 0
    await parser.analyze_apartments(stats=stats)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_cooldown_does_not_consume_budget_or_create_attempt_or_success(pipeline,monkeypatch):
    parser,details,_,_,_ = pipeline
    monkeypatch.setattr(details,"detail_fetch_cooldown_remaining",lambda:900)
    fetch = AsyncMock(side_effect=AssertionError("must not request"))
    monkeypatch.setattr(details,"fetch_apartment_details",fetch)
    stats = {"_detail_budget_remaining":1}
    result = (await parser.analyze_apartments(stats=stats))[0]
    assert stats["_detail_budget_remaining"] == 1
    assert "_source_details_attempted_at" not in result and "_source_details_checked_at" not in result
    fetch.assert_not_awaited()
