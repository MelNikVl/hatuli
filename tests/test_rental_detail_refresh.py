"""Stale descriptions require an actual budgeted fetch, never a local refresh."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import asyncpg
import pytest

from bot.core import apartment_details, complex_audit, rental_parser as parser
from bot.core.rental_furnishing import version_tag
from bot.core.rental_stage0_identity import observed_hex
from bot.db import pg, settings

NOW = datetime(2026,10,19,8,tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setenv("DATABASE_URL","postgresql://unused:unused@127.0.0.1:1/rental_detail_refresh_unit")
    async def deny(*args,**kwargs):
        raise AssertionError("No production database or source HTTP in detail refresh tests")
    monkeypatch.setattr(asyncpg,"connect",deny)
    monkeypatch.setattr(asyncpg,"create_pool",deny)
    monkeypatch.setattr(pg,"init_pool",deny)
    for name in ("fetch","fetchrow","fetchval","execute"):
        monkeypatch.setattr(pg,name,deny)
    monkeypatch.setattr(settings,"get",lambda key,default=None:default)
    fresh=parser._rental_description_is_fresh
    monkeypatch.setattr(parser,"_rental_description_is_fresh",lambda checked,*,now=None:fresh(checked,now=now or NOW))
    monkeypatch.setattr(parser.asyncio,"sleep",AsyncMock())
    monkeypatch.setattr(parser.random,"uniform",lambda *args:8.)
    monkeypatch.setattr(apartment_details,"REQUEST_COUNTS",{"detail":0})
    monkeypatch.setattr(apartment_details,"detail_fetch_cooldown_remaining",lambda:0)
    monkeypatch.setattr(complex_audit,"street_names",AsyncMock(return_value=set()))


def source(**changes):
    grid=observed_hex(51.128,71.430,50)
    return {"id":"123456","url":"https://krisha.kz/a/show/123456","lat":51.128,"lon":71.430,
        "complex_id":10,"complex_name":"Пример","complex_url":"https://krisha.kz/complex/show/10",
        "hex_id":grid["hex_id"],"hex_edge_m":grid["hex_edge_m"],"title":"Квартира",
        "description":"Полностью меблирована","description_checked_at":NOW-timedelta(hours=73),
        "furnishing_version":version_tag(),**changes}


def arrange(monkeypatch,row, response=None, error=None):
    read=AsyncMock(return_value=[row]);write=AsyncMock()
    monkeypatch.setattr(pg,"fetch",read);monkeypatch.setattr(pg,"execute",write)
    async def details(*args,**kwargs):
        apartment_details.REQUEST_COUNTS["detail"]+=1
        if error: raise error
        return response
    fetch=AsyncMock(side_effect=details)
    monkeypatch.setattr(apartment_details,"fetch_apartment_details",fetch)
    monkeypatch.setattr(parser,"resolve_rental_complex",AsyncMock(return_value={"complex_id":10,"status":"resolved","reason":"verified"}))
    return read,write,fetch


@pytest.mark.parametrize("age,expected",[(None,False),(timedelta(hours=72),True),
    (timedelta(hours=72,microseconds=1),False),(timedelta(seconds=-1),False),(timedelta(),True)])
def test_description_age_boundary_and_future_fail_closed(age,expected):
    checked=None if age is None else NOW-age
    assert parser._rental_description_is_fresh(checked,now=NOW) is expected


@pytest.mark.parametrize("value",["not-a-date",NOW.replace(tzinfo=None),0])
def test_invalid_description_timestamp_is_not_fresh(value):
    assert not parser._rental_description_is_fresh(value,now=NOW)


@pytest.mark.asyncio
@pytest.mark.parametrize("changes",[{}, {"furnishing_version":"old"}, {"hex_id":"wrong"},
    {"hex_id":"wrong","furnishing_version":"old"}, {"description_checked_at":None},
    {"description_checked_at":NOW+timedelta(seconds=1)}])
async def test_stale_description_fetches_even_when_catalog_filled_or_locally_reprojected(monkeypatch,changes):
    old=source(**changes)
    read,write,fetch=arrange(monkeypatch,old,response={"source_detail_valid":True,"description":"Без мебели"})
    result=await parser.backfill_rental_details(1,budget_seconds=30)
    assert fetch.await_count == 1 and fetch.await_args.args == (old["url"],)
    assert fetch.await_args.kwargs["raise_on_error"] is True
    assert 0 < fetch.await_args.kwargs["request_timeout_seconds"] <= 23
    assert result["checked"]==result["detail_attempts"]==result["requests"]==1
    successful=[c for c in write.await_args_list if "description_checked_at=now()" in c.args[0]]
    assert len(successful)==1 and "Без мебели" in successful[0].args and "none" in successful[0].args
    assert all("last_seen=" not in c.args[0] for c in write.await_args_list)
    assert not any("SET furnishing=$2,furnishing_version=$3" in c.args[0] for c in write.await_args_list)
    sql=read.await_args.args[0]
    assert "OR description_checked_at IS NULL" in sql
    assert "OR description_checked_at < now() - interval '72 hours'" in sql
    assert "OR description_checked_at > now()" in sql
    # Retry pacing is unchanged, independent of description freshness.
    assert "coord_fetch_attempted_at < now() - interval '3 days'" in sql
    assert "LIMIT $1" in sql and read.await_args.args[1]==1


@pytest.mark.asyncio
@pytest.mark.parametrize("changes",[{"furnishing_version":"old"},{"hex_id":"wrong"}])
async def test_fresh_description_keeps_local_rule_or_grid_work_without_source_fetch(monkeypatch,changes):
    row=source(description_checked_at=NOW-timedelta(hours=1),**changes)
    _,write,fetch=arrange(monkeypatch,row,error=AssertionError("Fresh local fix must not fetch"))
    result=await parser.backfill_rental_details(1,budget_seconds=30)
    fetch.assert_not_awaited()
    assert result["detail_attempts"]==result["checked"]==0
    assert write.await_count>=1
    assert all("description_checked_at" not in c.args[0] for c in write.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("response,error",[(None,None),({},None),(None,RuntimeError("upstream failed"))])
async def test_failed_or_empty_fetch_never_refreshes_old_description_timestamp(monkeypatch,response,error):
    _,write,fetch=arrange(monkeypatch,source(furnishing_version="old"),response=response,error=error)
    result=await parser.backfill_rental_details(1,budget_seconds=30)
    assert result["requests"]==1 and fetch.await_count==1
    assert write.await_count==1
    assert write.await_args.args[0]=="UPDATE rental_listings SET coord_fetch_attempted_at=now() WHERE id=$1"
    assert "description_checked_at" not in write.await_args.args[0]


@pytest.mark.asyncio
async def test_468_stops_and_marks_only_real_attempt_without_freshening(monkeypatch):
    _,write,fetch=arrange(monkeypatch,source(),error=apartment_details.ListingBlockedError("468 blocked"))
    pg.fetch.return_value=[source(),source(id="123457")]
    result=await parser.backfill_rental_details(2,budget_seconds=30)
    assert result["blocked"]==result["http_468"]==result["requests"]==1
    assert result["checked"]==0 and fetch.await_count==1
    write.assert_awaited_once_with("UPDATE rental_listings SET coord_fetch_attempted_at=now() WHERE id=$1", "123456")


@pytest.mark.asyncio
async def test_stale_fetch_timeout_preserves_description_and_remaining_candidates(monkeypatch):
    _,write,fetch=arrange(monkeypatch,source(furnishing_version="old"),error=TimeoutError())
    pg.fetch.return_value=[source(furnishing_version="old"),source(id="123457")]
    result=await parser.backfill_rental_details(2,budget_seconds=30)
    assert result["budget_exhausted"] is True and result["errors"]==1
    assert result["checked"]==0 and result["requests"]==1 and fetch.await_count==1
    write.assert_awaited_once_with("UPDATE rental_listings SET coord_fetch_attempted_at=now() WHERE id=$1", "123456")


@pytest.mark.asyncio
async def test_detail_cooldown_does_not_even_read_stale_candidates(monkeypatch):
    read,write,fetch=arrange(monkeypatch,source())
    monkeypatch.setattr(apartment_details,"detail_fetch_cooldown_remaining",lambda:900)
    result=await parser.backfill_rental_details(1,budget_seconds=30)
    assert result["cooldown"] is True
    read.assert_not_awaited();write.assert_not_awaited();fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_small_budget_cannot_refresh_via_rule_update_or_start_source_request(monkeypatch):
    _,write,fetch=arrange(monkeypatch,source(furnishing_version="old"))
    result=await parser.backfill_rental_details(1,budget_seconds=10)
    assert result["budget_exhausted"] is True
    write.assert_not_awaited();fetch.assert_not_awaited();parser.asyncio.sleep.assert_not_awaited()
