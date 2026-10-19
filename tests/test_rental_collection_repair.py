"""Operational invariants, with mocked source/SQL and a private temporary state."""
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from bot.core import apartment_details as details, complex_audit, detail_cooldown
from bot.core import rental_parser as parser
from bot.core.rental_service_metrics import RentalCyclePlan, rental_list_batch_size
from bot.db import pg, settings
import service_rental as service

PATH = "/arenda/kvartiry/astana/"
KEY = "RENTAL_CRAWL_V1_APARTMENT"


@pytest.mark.parametrize("pages,interval,maximum,expected", [
    (None,None,8,8), ("unknown",None,8,8), (216,None,8,3),
    (575,None,8,6), (30,None,8,1), (575,1200,8,8),
    (575,1200,4,4), (575,600,8,4), (216,800,8,2),
    (575,float("nan"),8,6), (575,600,999,4),
])
def test_adaptive_batch_weights_catalog_size_without_changing_request_pace(pages,interval,maximum,expected):
    assert rental_list_batch_size(pages,interval,maximum=maximum) == expected


@pytest.fixture(autouse=True)
def private_state(monkeypatch, tmp_path):
    monkeypatch.setenv("HATULI_DETAIL_COOLDOWN_PATH", str(tmp_path / "cooldown.json"))
    monkeypatch.setattr(details, "_BLOCKED_UNTIL", 0.)
    monkeypatch.setattr(details, "_BLOCKED_STREAK", 0)
    monkeypatch.setattr(details, "REQUEST_COUNTS", {"detail": 0})
    monkeypatch.setattr(parser.asyncio, "sleep", AsyncMock())


def catalog(n, *, last=5):
    return (f'<h1>Найдено {last} объявлений</h1><div class="a-list a-search-list">'
        f'<div class="a-card" data-id="{100+n}"><a class="a-card__title" href="/a/show/{100+n}">'
        '2-комнатная, 50 м²</a><div class="a-card__price">200 000</div></div></div>'
        f'<nav class="paginator"><a href="?page={last}" rel="last">{last}</a></nav>')


@pytest.fixture
def cursor(monkeypatch):
    state = {}
    monkeypatch.setattr(settings, "load", AsyncMock())
    monkeypatch.setattr(settings, "get", lambda key, default="": state.get(key, default))
    async def save(key, value): state[key] = value
    monkeypatch.setattr(settings, "set", save)
    monkeypatch.setattr(parser, "save_rental_listings", AsyncMock(return_value=1))
    return state


@pytest.mark.asyncio
async def test_batch_reuses_head_and_durably_saves_each_page(cursor):
    calls = []
    def response(request):
        n = int(request.url.params.get("page", 1)); calls.append(n)
        return httpx.Response(200, text=catalog(n))
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        result = await parser.collect_rental_page(client, PATH, "apartment", refresh_head=True,
            max_durable_pages=4, head_min_interval_seconds=2700)
    assert calls == [1,2,3,4]
    assert result["durable_pages"] == 4 and result["head_pages"] == 1
    assert parser.RentalPagination.loads(cursor[KEY]).page == 5
    assert parser.asyncio.sleep.await_count == 3


@pytest.mark.asyncio
async def test_recent_head_is_not_repeated_and_batch_stops_at_actual_end(cursor):
    c = parser.RentalPagination(page=4, pages_seen=3, started_from_first=True)
    cursor[KEY] = c.dumps()
    cursor[f"{KEY}_HEAD_CHECKED_AT"] = datetime.now(timezone.utc).isoformat()
    calls = []
    def response(request):
        n = int(request.url.params.get("page", 1)); calls.append(n)
        return httpx.Response(200, text=catalog(n))
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        result = await parser.collect_rental_page(client, PATH, "apartment", refresh_head=True,
            max_durable_pages=8, head_min_interval_seconds=2700)
    assert calls == [4,5] and result["completed"]
    assert result["durable_pages"] == 2 and result["head_pages"] == 0
    assert cursor[f"{KEY}_COMPLETED_VERIFIED"] == "1"


@pytest.mark.asyncio
async def test_later_page_failure_keeps_previous_checkpoints_and_metrics(cursor):
    def response(request):
        n = int(request.url.params.get("page", 1))
        return httpx.Response(468 if n == 3 else 200, text=catalog(n))
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        with pytest.raises(httpx.HTTPStatusError) as failure:
            await parser.collect_rental_page(client, PATH, "apartment", refresh_head=True,
                max_durable_pages=8)
    assert parser.RentalPagination.loads(cursor[KEY]).page == 3
    assert failure.value.rental_metrics["durable_pages"] == 2
    assert failure.value.rental_metrics["requests"] == 3
    assert f"{KEY}_COMPLETED_AT" not in cursor


def test_cooldown_survives_restart_and_success_cannot_clear_active_deadline(monkeypatch):
    remaining, streak = detail_cooldown.record_block()
    assert remaining == 900 and streak == 1
    monkeypatch.setattr(details, "_BLOCKED_UNTIL", 0.)
    monkeypatch.setattr(details, "_BLOCKED_STREAK", 0)
    assert 899 < details.detail_fetch_cooldown_remaining() <= 900
    detail_cooldown.record_success()
    assert detail_cooldown.read_cooldown()[0] > 899
    until = datetime.now(timezone.utc) + timedelta(seconds=901)
    remaining, streak = detail_cooldown.record_block(now=until)
    assert remaining == 1800 and streak == 2


@pytest.mark.parametrize("payload", ["{", "null", '{"version":1,"streak":1,"blocked_until":"2026-01-01"}'])
def test_invalid_backoff_state_fails_closed(payload):
    detail_cooldown.state_path().write_text(payload, encoding="utf-8")
    assert details.detail_fetch_cooldown_remaining() > 21599


def test_exponential_backoff_never_reduces_deadline_and_stays_six_hours_at_cap():
    now = datetime.now(timezone.utc)
    for expected in (900,1800,3600,7200,14400,21600,21600):
        remaining, _ = detail_cooldown.record_block(now=now)
        assert remaining == expected
    # A stale success from another worker cannot override the active deadline.
    detail_cooldown.record_success(now=now+timedelta(seconds=1))
    assert detail_cooldown.read_cooldown(now=now)[0] == 21600


@pytest.mark.asyncio
async def test_actual_http_timeout_is_counted_before_transport_completes(monkeypatch):
    constructor = httpx.AsyncClient
    def failed(request): raise httpx.ReadTimeout("mock timeout", request=request)
    monkeypatch.setattr(details.httpx, "AsyncClient", lambda **kwargs:
        constructor(transport=httpx.MockTransport(failed), **kwargs))
    with pytest.raises(httpx.ReadTimeout):
        await details.fetch_apartment_details("https://krisha.kz/a/show/123456", raise_on_error=True,
            request_timeout_seconds=7)
    assert details.REQUEST_COUNTS["detail"] == 1


@pytest.mark.asyncio
async def test_200_challenge_is_a_durable_block_not_a_success(monkeypatch):
    constructor = httpx.AsyncClient
    monkeypatch.setattr(details.httpx, "AsyncClient", lambda **kwargs:
        constructor(transport=httpx.MockTransport(lambda request:
            httpx.Response(200, text="<h1>Подтвердите, что вы не робот</h1>")), **kwargs))
    with pytest.raises(details.ListingBlockedError, match="200 challenge"):
        await details.fetch_apartment_details("https://krisha.kz/a/show/123456", raise_on_error=True)
    assert detail_cooldown.read_cooldown()[0] > 899
    assert details.REQUEST_COUNTS["detail"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("canonical", ["123456","123457"])
async def test_recognized_offer_requires_same_source_identity(monkeypatch,canonical):
    constructor = httpx.AsyncClient
    html = ('<h1>2-комнатная квартира</h1><div class="offer__price">200 000</div>'
        f'<link rel="canonical" href="https://krisha.kz/a/show/{canonical}">')
    monkeypatch.setattr(details.httpx,"AsyncClient",lambda **kwargs:constructor(
        transport=httpx.MockTransport(lambda request:httpx.Response(200,text=html)),**kwargs))
    result = await details.fetch_apartment_details("https://krisha.kz/a/show/123456",raise_on_error=True)
    assert result["source_detail_valid"] is (canonical == "123456")


@pytest.mark.asyncio
async def test_redirect_to_other_listing_cannot_be_a_fresh_detail_success(monkeypatch):
    constructor = httpx.AsyncClient
    def serve(request):
        if request.url.path.endswith("123456"):
            return httpx.Response(302,headers={"location":"https://krisha.kz/a/show/123457"})
        return httpx.Response(200,text='<h1>Другая квартира</h1><div class="offer__price">200000</div>')
    monkeypatch.setattr(details.httpx,"AsyncClient",lambda **kwargs:constructor(
        transport=httpx.MockTransport(serve),**kwargs))
    result = await details.fetch_apartment_details("https://krisha.kz/a/show/123456",raise_on_error=True)
    assert result["source_detail_valid"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("canonical", ["https://krisha.kz/", "https://elsewhere.example/a/show/123456", ""])
async def test_present_invalid_or_external_canonical_cannot_be_ignored(monkeypatch,canonical):
    constructor = httpx.AsyncClient
    html = ('<h1>Квартира</h1><div class="offer__price">200000</div>'
        f'<link rel="canonical" href="{canonical}">')
    monkeypatch.setattr(details.httpx,"AsyncClient",lambda **kwargs:constructor(
        transport=httpx.MockTransport(lambda request:httpx.Response(200,text=html)),**kwargs))
    result = await details.fetch_apartment_details("https://krisha.kz/a/show/123456",raise_on_error=True)
    assert result["source_detail_valid"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["https://elsewhere.example/a/show/123456", "http://krisha.kz/a/show/123456"])
async def test_redirect_to_same_id_at_untrusted_origin_is_not_fresh(monkeypatch,target):
    constructor = httpx.AsyncClient
    def serve(request):
        if str(request.url) == "https://krisha.kz/a/show/123456":
            return httpx.Response(302,headers={"location":target})
        return httpx.Response(200,text='<h1>Квартира</h1><div class="offer__price">200000</div>')
    monkeypatch.setattr(details.httpx,"AsyncClient",lambda **kwargs:constructor(
        transport=httpx.MockTransport(serve),**kwargs))
    result = await details.fetch_apartment_details("https://krisha.kz/a/show/123456",raise_on_error=True)
    assert result["source_detail_valid"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("canonical", ["/a/show/123456", "https://www.krisha.kz/a/show/123456/"])
async def test_trusted_same_id_absolute_or_relative_canonical_remains_valid(monkeypatch,canonical):
    constructor = httpx.AsyncClient
    html = ('<h1>Квартира</h1><div class="offer__price">200000</div>'
        f'<link rel="canonical" href="{canonical}">')
    monkeypatch.setattr(details.httpx,"AsyncClient",lambda **kwargs:constructor(
        transport=httpx.MockTransport(lambda request:httpx.Response(200,text=html)),**kwargs))
    result = await details.fetch_apartment_details("https://krisha.kz/a/show/123456",raise_on_error=True)
    assert result["source_detail_valid"] is True


@pytest.fixture
def queue(monkeypatch):
    row = {"id":"123456", "url":"https://krisha.kz/a/show/123456", "title":"2-комнатная",
        "complex_id":10, "description_checked_at":datetime.now(timezone.utc)-timedelta(days=4),
        "queue_due":100, "oldest_due_age_hours":200}
    read = AsyncMock(return_value=[row]); write = AsyncMock()
    monkeypatch.setattr(pg,"fetch",read); monkeypatch.setattr(pg,"execute",write)
    monkeypatch.setattr(settings,"get",lambda key,default=None:default)
    monkeypatch.setattr(complex_audit,"street_names",AsyncMock(return_value=set()))
    monkeypatch.setattr(parser.random,"uniform",lambda *_:8.)
    monkeypatch.setattr(parser,"resolve_rental_complex",AsyncMock(return_value={
        "complex_id":None,"status":"unresolved","reason":"source_name_unverified"}))
    return row, read, write


@pytest.mark.asyncio
async def test_scoped_fifo_and_success_replaces_disproved_fk_without_inventing_freshness(queue,monkeypatch):
    row, read, write = queue
    async def source(*args,**kwargs):
        details.REQUEST_COUNTS["detail"] += 1
        assert .5 <= kwargs["request_timeout_seconds"] <= 30
        return {"source_detail_valid":True,"description":"Без мебели", "complex_source":"official_map_block",
            "complex_name":"Неизвестный дом","complex_url":"https://krisha.kz/complex/show/123"}
    monkeypatch.setattr(details,"fetch_apartment_details",source)
    result = await parser.backfill_rental_details(1,budget_seconds=45,
        prop_type="apartment",rent_period="monthly")
    assert result["success"] == result["requests"] == 1 and result["status"] == "success"
    assert result["queue_scope"] == "apartment" and result["eligible_due"] == 100
    assert read.await_args.args[-2:] == ("apartment","monthly")
    assert "found_at ASC NULLS FIRST, id" in read.await_args.args[0]
    sql = write.await_args.args[0]
    assert "complex_id=COALESCE" not in sql and "description_checked_at=$" in sql
    assert "last_seen=" not in sql and None in write.await_args.args


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [{"description":"unrecognized shell"},
    {"source_detail_valid":True,"is_archived":True,"description":"old"}])
async def test_invalid_or_archived_detail_does_not_refresh_timestamp(queue,monkeypatch,reply):
    async def source(*args,**kwargs):
        details.REQUEST_COUNTS["detail"] += 1
        return reply
    monkeypatch.setattr(details,"fetch_apartment_details",source)
    result = await parser.backfill_rental_details(1,budget_seconds=45)
    assert result["success"] == 0 and result["errors"] == 1
    assert write_only_attempt(queue[2])


def write_only_attempt(write):
    return write.await_count == 1 and write.await_args.args[0] == (
        "UPDATE rental_listings SET coord_fetch_attempted_at=now() WHERE id=$1")


@pytest.mark.asyncio
async def test_real_block_rotates_only_attempted_head_and_preserves_old_detail(queue,monkeypatch):
    queue[1].return_value = [queue[0], {**queue[0], "id":"123457"}]
    async def source(*args,**kwargs):
        details.REQUEST_COUNTS["detail"] += 1
        raise details.ListingBlockedError("468 blocked")
    monkeypatch.setattr(details,"fetch_apartment_details",source)
    result = await parser.backfill_rental_details(2,budget_seconds=45)
    assert result["status"] == "blocked" and result["requests"] == 1
    assert result["success"] == 0 and write_only_attempt(queue[2])
    assert queue[2].await_args.args[-1] == "123456"


def ops():
    return service.RentalOperations(
        AsyncMock(return_value={"page":8,"saved":160,"durable_pages":8,"head_pages":0,
            "requests":8,"completed":False,"expected_last_page":575}),
        AsyncMock(),AsyncMock(),AsyncMock(),AsyncMock(return_value={"requests":1,"success":1}),
        AsyncMock(return_value={"requests":0}),AsyncMock(),AsyncMock(),AsyncMock())


@pytest.mark.asyncio
async def test_monthly_two_turns_daily_one_and_cooldown_does_not_consume_turn(monkeypatch):
    monkeypatch.setattr(service,"record_rental_cycle",AsyncMock())
    operations = ops(); state = service.RentalServiceState()
    scopes = []
    async def backfill(*args,**kwargs):
        scopes.append(kwargs["rent_period"])
        return {"requests":1,"success":1,"status":"success"}
    operations.backfill = backfill
    plan = RentalCyclePlan(archive_batch=0)
    for _ in range(3):
        await service.run_one_cycle(None,PATH,"apartment",state,operations,plan)
    assert scopes == ["monthly","monthly","daily"] and state.detail_turn == 0
    operations.backfill = AsyncMock(return_value={"requests":0,"status":"cooldown","cooldown":True})
    cycle = await service.run_one_cycle(None,PATH,"apartment",state,operations,plan)
    assert state.detail_turn == 0 and cycle["detail"]["status"] == "cooldown"
    operations.backfill = AsyncMock(return_value={"requests":0,"status":"idle","eligible_due":0})
    await service.run_one_cycle(None,PATH,"apartment",state,operations,plan)
    assert state.detail_turn == 1  # an empty monthly queue cannot starve daily


@pytest.mark.asyncio
async def test_batch_rate_accounts_for_eight_pages_and_reports_missed_target(monkeypatch):
    monkeypatch.setattr(service,"record_rental_cycle",AsyncMock())
    operations = ops(); state = service.RentalServiceState()
    now = [0.]
    plan = RentalCyclePlan(backfill_batch=0,archive_batch=0)
    await service.run_one_cycle(None,PATH,"apartment",state,operations,plan,clock=lambda:now[0])
    now[0] = 800
    cycle = await service.run_one_cycle(None,PATH,"apartment",state,operations,plan,clock=lambda:now[0])
    assert cycle["page_interval_seconds"] == 100
    assert cycle["estimated_circle_hours"] == 15.97
    assert cycle["traversal_target_status"] == "within_target_estimate"
    now[0] += 4000
    cycle = await service.run_one_cycle(None,PATH,"apartment",state,operations,plan,clock=lambda:now[0])
    assert cycle["estimated_circle_hours"] > 24
    assert cycle["traversal_target_status"] == "missed_target_estimate"


@pytest.mark.asyncio
async def test_partial_batch_counts_only_saved_progress_and_no_detail_on_block(monkeypatch):
    monkeypatch.setattr(service,"record_rental_cycle",AsyncMock())
    operations = ops(); state = service.RentalServiceState()
    error = parser.RentalSearchBlockedError("challenge")
    error.rental_metrics = {"durable_pages":3,"saved":60,"head_pages":1,"requests":5}
    operations.collect.side_effect = error
    cycle = await service.run_one_cycle(None,PATH,"apartment",state,operations,RentalCyclePlan())
    assert cycle["durable_pages"] == state.pages_since_index == 3
    assert cycle["traversal_target_status"] == "unknown"
    operations.backfill.assert_not_awaited()


@pytest.mark.asyncio
async def test_database_work_reserves_request_window_in_normal_45_second_plan(monkeypatch):
    monkeypatch.setattr(service,"record_rental_cycle",AsyncMock())
    operations = ops(); state = service.RentalServiceState(cycle=4,index_refresh_required=True)
    now = [0.]
    async def index(): now[0] += 15
    async def backfill(*args,**kwargs):
        assert kwargs["budget_seconds"] == 30
        return {"requests":1,"success":1,"status":"success"}
    operations.rebuild_index = index; operations.backfill = AsyncMock(side_effect=backfill)
    cycle = await service.run_one_cycle(None,PATH,"apartment",state,operations,
        RentalCyclePlan(archive_batch=0),clock=lambda:now[0])
    operations.backfill.assert_awaited_once()
    operations.dedup.assert_not_awaited(); operations.bind_address.assert_not_awaited()
    assert cycle["detail"]["success"] == 1


@pytest.mark.asyncio
async def test_realistic_pacing_leaves_explicit_transport_budget_in_45s(queue,monkeypatch):
    now = [0.]
    monkeypatch.setattr(parser.time,"monotonic",lambda:now[0])
    monkeypatch.setattr(parser.random,"uniform",lambda *_:15.)
    async def pause(seconds): now[0] += seconds
    monkeypatch.setattr(parser.asyncio,"sleep",pause)
    async def source(*args,**kwargs):
        assert kwargs["request_timeout_seconds"] == 23
        now[0] += 6 + 10  # shared pacing then successful transport
        details.REQUEST_COUNTS["detail"] += 1
        return {"source_detail_valid":True,"description":"Без мебели"}
    monkeypatch.setattr(details,"fetch_apartment_details",source)
    result = await parser.backfill_rental_details(1,budget_seconds=45)
    assert result["success"] == result["requests"] == 1 and now[0] == 31


@pytest.mark.asyncio
async def test_timeout_marks_only_real_started_attempt_with_old_description_untouched(queue,monkeypatch):
    async def source(*args,**kwargs):
        details.REQUEST_COUNTS["detail"] += 1
        raise httpx.ReadTimeout("mocked source timeout")
    monkeypatch.setattr(details,"fetch_apartment_details",source)
    result = await parser.backfill_rental_details(1,budget_seconds=45)
    assert result["requests"] == result["errors"] == 1 and result["success"] == 0
    assert write_only_attempt(queue[2])
