"""Rental source fields and list backoff: synthetic HTTP, SQL mocks, no live DB."""
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import json
from pathlib import Path
import re
from unittest.mock import AsyncMock

import asyncpg
import httpx
import pytest

from bot.core import apartment_details, complex_audit, rental_parser as parser
from bot.core.rental_list_backoff import RentalCatalogCooldown, blocked_state, read_backoff
from bot.core.rental_service_metrics import RentalCyclePlan
from bot.core.rental_source_details import rental_source_details, RentalSourceDetailsMigrationRequired
from bot.db import pg, settings
from bot.rental_detail_web import project_rental_detail
import service_rental as service

PATH = "/arenda/kvartiry/astana/"
KEY = "RENTAL_CRAWL_V1_APARTMENT"
NOW = datetime(2026,10,19,12,tzinfo=timezone.utc)
PHOTO = "https://astps-photos-kr.kcdn.kz/webp/97/975ab662-be68-44d9-9ab1-9af315e70c7a/23-full.webp"


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setenv("DATABASE_URL","postgresql://unused:unused@127.0.0.1:1/rental_refresh_unit")
    async def deny(*args,**kwargs): raise AssertionError("No real DB in source-details units")
    monkeypatch.setattr(asyncpg,"connect",deny); monkeypatch.setattr(asyncpg,"create_pool",deny)
    monkeypatch.setattr(pg,"init_pool",deny)
    for key in ("fetch","fetchrow","fetchval","execute"):
        monkeypatch.setattr(pg,key,deny)
    monkeypatch.setattr(parser.asyncio,"sleep",AsyncMock())


@pytest.fixture
def state(monkeypatch):
    values = {}
    monkeypatch.setattr(settings,"load",AsyncMock())
    monkeypatch.setattr(settings,"get",lambda key,default="":values.get(key,default))
    async def write(key,value): values[key] = value
    monkeypatch.setattr(settings,"set",AsyncMock(side_effect=write))
    monkeypatch.setattr(parser,"save_rental_listings",AsyncMock(return_value=0))
    return values


@pytest.mark.asyncio
@pytest.mark.parametrize("status",[403,429,468,200])
async def test_catalog_block_persists_retry_after_and_survives_next_call_without_get(state,status):
    state[KEY] = parser.RentalPagination(page=4).dumps()
    original = state[KEY]
    visits = []
    def serve(request):
        visits.append(str(request.url))
        return httpx.Response(status,headers={"retry-after":"7200"},text="Подтвердите, что вы не робот")
    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        with pytest.raises((httpx.HTTPStatusError,parser.RentalSearchBlockedError)):
            await parser.collect_rental_page(client,PATH,"apartment",max_durable_pages=8)
        _, remaining = read_backoff(state[f"{KEY}_LIST_BACKOFF"])
        assert 7190 < remaining <= 7200
        with pytest.raises(RentalCatalogCooldown) as paused:
            await parser.collect_rental_page(client,PATH,"apartment",refresh_head=True,max_durable_pages=8)
    assert len(visits) == 1 and state[KEY] == original
    assert paused.value.rental_metrics["requests"] == paused.value.rental_metrics["durable_pages"] == 0


def test_retry_after_date_longer_than_six_hours_is_not_truncated():
    future = NOW+timedelta(hours=12)
    raw = blocked_state("",format_datetime(future,usegmt=True),now=NOW)
    assert read_backoff(raw,now=NOW)[1] == 12*3600
    assert read_backoff(blocked_state(raw,"nonsense",now=NOW),now=NOW)[1] == 12*3600


@pytest.mark.asyncio
async def test_monthly_pause_does_not_become_another_monthly_cursor_or_stop_healthy_daily(state):
    state[f"{KEY}_LIST_BACKOFF"] = blocked_state("")
    calls = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:
            calls.append(req.url.path) or httpx.Response(200,text='<div class="a-list-empty">Ничего не найдено</div>'))) as client:
        with pytest.raises(RentalCatalogCooldown):
            await parser.collect_rental_page(client,PATH,"apartment")
        result = await parser.collect_rental_page(client,"/arenda/kvartiry-posutochno/astana/","apartment")
    assert len(calls) == 1 and "posutochno" in calls[0] and result["completed"]


@pytest.mark.asyncio
async def test_legacy_fullscan_also_honours_same_catalog_pause(state,monkeypatch):
    state[f"{KEY}_LIST_BACKOFF"] = blocked_state("")
    source = AsyncMock()
    monkeypatch.setattr(parser,"_fetch_page_result",source)
    with pytest.raises(RentalCatalogCooldown):
        await parser.parse_rental_path(PATH,"apartment")
    source.assert_not_awaited()


@pytest.mark.asyncio
async def test_corrupt_state_fails_closed_but_valid_expired_state_can_recover(state):
    state[f"{KEY}_LIST_BACKOFF"] = "null"
    visits = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:
            visits.append(req.url.path) or httpx.Response(200,text='<div class="a-list-empty">Ничего не найдено</div>'))) as client:
        with pytest.raises(RentalCatalogCooldown):
            await parser.collect_rental_page(client,PATH,"apartment")
        assert not visits
        state[f"{KEY}_LIST_BACKOFF"] = blocked_state("",now=datetime.now(timezone.utc)-timedelta(hours=1))
        result = await parser.collect_rental_page(client,PATH,"apartment")
    assert result["completed"] and state[f"{KEY}_LIST_BACKOFF"] == ""


@pytest.mark.asyncio
async def test_service_cooldown_counts_zero_source_gets_and_skips_maintenance(monkeypatch):
    persisted = AsyncMock(); monkeypatch.setattr(service,"record_rental_cycle",persisted)
    error = RentalCatalogCooldown(1800)
    error.rental_metrics = {"requests":0,"durable_pages":0,"cooldown_remaining_seconds":1800}
    ops = service.RentalOperations(AsyncMock(side_effect=error),*[AsyncMock() for _ in range(8)])
    cycle = await service.run_one_cycle(None,PATH,"apartment",service.RentalServiceState(),ops,RentalCyclePlan())
    assert cycle["list_status"] == "cooldown" and cycle["list_cooldown_remaining_seconds"] == 1800
    assert persisted.await_args.args[1]["list_requests"] == 0
    assert persisted.await_args.args[1]["list_cooldown_cycles"] == 1
    assert persisted.await_args.args[1].get("blocked",0) == 0
    ops.backfill.assert_not_awaited(); ops.archive.assert_not_awaited()


def full(**changes):
    return {"source_detail_valid":True,"listing_identity":{"rooms":2,"area":61.,"price":230000},
        "floor":8,"floors_total":11,"year_built":2020,"kitchen_area":13.,"ceiling_height":3.,
        "renovation_param":"Хорошее","furniture_param":"Полностью", "description_observed":True,
        "description":"Полностью меблирована. private-contact", "phone":"private-contact",
        "raw_html":"private-html", "photos":[PHOTO,"https://evil.example/photo.jpg"], **changes}


def test_snapshot_is_explicit_bounded_and_excludes_contacts_and_raw_text():
    snapshot = rental_source_details(full(),now=NOW)
    assert snapshot["rooms"] == 2 and snapshot["price"] == 230000
    assert snapshot["condition"] == "good" and snapshot["furniture"] == "full"
    assert snapshot["year_built"] == 2020 and snapshot["kitchen_area"] == 13
    assert not any(word in json.dumps(snapshot) for word in ("private-contact","private-html","description\"","phone","photos"))


@pytest.mark.parametrize("changes",[{"source_detail_valid":False},{"is_archived":True},
    {"listing_identity":{"sale_astana":True,"price":230000}}])
def test_unverified_archived_or_explicit_sale_is_not_rental_snapshot(changes):
    assert rental_source_details(full(**changes),now=NOW) is None


def test_invalid_and_contradictory_numeric_values_are_not_confirmed_fields():
    snapshot = rental_source_details(full(floor=20,floors_total=11,year_built=3000,kitchen_area=200,
        ceiling_height=float("nan"),listing_identity={"rooms":True,"area":50,"price":-1}),now=NOW)
    assert set(snapshot).isdisjoint({"rooms","price","floor","floors_total","year_built","kitchen_area","ceiling_height"})


@pytest.fixture
def refresh(monkeypatch):
    read = AsyncMock(return_value=[{"id":"123456", "url":"https://krisha.kz/a/show/123456", "title":"Квартира"}])
    write = AsyncMock(return_value="UPDATE 1")
    monkeypatch.setattr(pg,"fetch",read); monkeypatch.setattr(pg,"execute",write)
    monkeypatch.setattr(settings,"get",lambda key,default="":default)
    monkeypatch.setattr(complex_audit,"street_names",AsyncMock(return_value=set()))
    monkeypatch.setattr(apartment_details,"detail_fetch_cooldown_remaining",lambda:0)
    monkeypatch.setattr(apartment_details,"REQUEST_COUNTS",{"detail":0})
    monkeypatch.setattr(parser.random,"uniform",lambda *_:8.)
    monkeypatch.setattr(parser,"resolve_rental_complex",AsyncMock(return_value={"complex_id":None}))
    return read,write


def value_for(call,field):
    index = int(re.search(rf"\b{field}=\$(\d+)",call.args[0])[1])
    return call.args[index]


@pytest.mark.asyncio
async def test_full_success_refreshes_fields_and_snapshot_atomically_without_price_writer(refresh,monkeypatch):
    async def source(*args,**kwargs):
        apartment_details.REQUEST_COUNTS["detail"] += 1
        return full()
    monkeypatch.setattr(apartment_details,"fetch_apartment_details",source)
    result = await parser.backfill_rental_details(1,budget_seconds=45)
    call = refresh[1].await_args
    snapshot = json.loads(value_for(call,"source_details"))
    assert snapshot["price"] == 230000 and value_for(call,"rooms") == 2
    assert value_for(call,"area") == 61 and value_for(call,"floor") == 8 and value_for(call,"floors_total") == 11
    assert json.loads(value_for(call,"photos")) == [PHOTO]
    assert value_for(call,"description_checked_at").tzinfo is not None
    assert "description_checked_at<=" in call.args[0]
    assert "last_seen<=" in call.args[0]
    assert "price=" not in call.args[0] and "last_seen=" not in call.args[0] and "rental_price_history" not in call.args[0]
    assert result["success"] == 1


@pytest.mark.asyncio
async def test_superseded_full_observation_does_not_claim_success_or_rewrite_newer_values(refresh,monkeypatch):
    refresh[1].return_value = "UPDATE 0"
    monkeypatch.setattr(apartment_details,"fetch_apartment_details",AsyncMock(return_value=full()))
    result = await parser.backfill_rental_details(1,budget_seconds=45)
    assert result["success"] == 0
    assert "last_seen<=" in refresh[1].await_args.args[0] and "description_checked_at<=" in refresh[1].await_args.args[0]


@pytest.mark.asyncio
async def test_mismatch_cannot_overwrite_fields_price_or_snapshot(refresh,monkeypatch):
    monkeypatch.setattr(apartment_details,"fetch_apartment_details",AsyncMock(return_value=full(source_detail_valid=False)))
    result = await parser.backfill_rental_details(1,budget_seconds=45)
    assert result["success"] == 0
    assert refresh[1].await_args.args[0] == "UPDATE rental_listings SET coord_fetch_attempted_at=now() WHERE id=$1"


@pytest.mark.asyncio
async def test_missing_114_stops_before_source_request(refresh,monkeypatch):
    refresh[0].side_effect = asyncpg.UndefinedColumnError('column "source_details" does not exist')
    source = AsyncMock(); monkeypatch.setattr(apartment_details,"fetch_apartment_details",source)
    with pytest.raises(RentalSourceDetailsMigrationRequired,match="114_rental_source_details"):
        await parser.backfill_rental_details(1,budget_seconds=45)
    source.assert_not_awaited(); refresh[1].assert_not_awaited()


def test_private_detail_api_does_not_promote_source_snapshot_price():
    row = {"id":"123456","price":200000,"source_details":{"price":999999,"phone":"private-contact"}}
    public = project_rental_detail(row,[])
    assert public["price"] == 200000 and "source_details" not in public
    assert "private-contact" not in json.dumps(public)


@pytest.mark.asyncio
async def test_catalog_null_attributes_preserve_previous_detail_columns(monkeypatch):
    write = AsyncMock(return_value={"is_new":False,"reactivated":False})
    monkeypatch.setattr(pg,"fetchrow",write); monkeypatch.setattr(parser,"resolve_rental_complex",AsyncMock(return_value=None))
    listing = parser.RentalListing("123456","https://krisha.kz/a/show/123456","Квартира",200000,None,None,None,None,
        "",None,None,"astana","apartment","")
    assert await parser.save_rental_listings([listing]) == 1
    sql = write.await_args.args[0]
    for field in ("area","rooms","floor","floors_total"):
        assert f"{field} = COALESCE(EXCLUDED.{field}, rental_listings.{field})" in sql
    assert "source_details" not in sql and "description_checked_at" not in sql


def test_migration_has_nullable_snapshot_without_fake_backfill_or_second_timestamp():
    sql = (Path(__file__).resolve().parents[1]/"migrations/114_rental_source_details.sql").read_text(encoding="utf-8")
    statements = re.sub(r"--[^\n]*","",sql).upper()
    assert "ADD COLUMN IF NOT EXISTS SOURCE_DETAILS JSONB" in statements
    assert not any(word in statements for word in ("UPDATE "," DEFAULT ","DETAIL_CHECKED_AT","INSERT "))
