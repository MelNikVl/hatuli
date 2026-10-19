"""Endpoint, refresh and map SQL tests with all database operations mocked."""
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from dataclasses import asdict
import json
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from bot.core import rental_economics_refresh as refresh
from bot.core import rental_economics_filters as filters
from bot.core.rental_economics import build_rental_economics, attach_benchmark, benchmark_scopes
from bot.core.rental_yield import NetYieldParams
from bot.web import rental_economics as route


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setenv("DATABASE_URL","postgresql://unused:unused@127.0.0.1:1/rental_stage1_test")
    monkeypatch.setattr("dotenv.load_dotenv",lambda *a,**k:False)
    denied=AsyncMock(side_effect=AssertionError("No database or migration"))
    monkeypatch.setattr("asyncpg.connect",denied); monkeypatch.setattr("asyncpg.create_pool",denied)
    monkeypatch.setattr("bot.db.pg.init_pool",denied)
    monkeypatch.setattr("bot.db.settings.get_int",lambda key,default:default)


def sale(identifier="10000001"):
    now=datetime.now(timezone.utc)
    return {"id":identifier,"price":40000000,"area":60.0,"rooms":2,"complex_id":10,"complex_name":"ЖК",
            "complex_resolution":"url","resolved_house_id":None,"house_attribution":None,"district":"Есиль",
            "is_active":True,"is_duplicate":False,"dup_needs_review":False,"archived_at":None,"duplicate_of":None,
            "last_seen":now-timedelta(minutes=5),"first_seen":now-timedelta(days=3)}


def ix():
    now=datetime.now(timezone.utc)
    return {"level":"район","median_price":300000,"p25_price":250000,"p75_price":350000,"price_per_sqm":5000,
            "sample_count":10,"rent_period":"monthly","policy_version":"3","window_days":30,
            "updated_at":now-timedelta(minutes=1),"source_oldest_at":now-timedelta(hours=3),"source_newest_at":now-timedelta(hours=1)}


@pytest.mark.parametrize("tier,expected",[("public",403),("subscriber",204),("admin",204)])
def test_endpoint_existing_access_gate_and_missing_block(monkeypatch,tier,expected):
    monkeypatch.setattr(route,"get_user_tier",AsyncMock(return_value=tier))
    fetch=AsyncMock(return_value=sale()); monkeypatch.setattr("bot.db.pg.fetchrow",fetch)
    monkeypatch.setattr("bot.core.rental_economics.rental_economics_for_listing",AsyncMock(return_value=None))
    app=FastAPI(); app.include_router(route.router)
    response=TestClient(app).get("/api/listing/10000001/rental-economics")
    assert response.status_code==expected and response.headers["cache-control"]=="no-store"
    if tier=="public": fetch.assert_not_awaited()
    else: assert response.content==b""


def test_endpoint_404_and_ready_projection_hides_private_facts(monkeypatch):
    monkeypatch.setattr(route,"get_user_tier",AsyncMock(return_value="subscriber"))
    fetch=AsyncMock(return_value=None); monkeypatch.setattr("bot.db.pg.fetchrow",fetch)
    app=FastAPI(); app.include_router(route.router); client=TestClient(app)
    assert client.get("/api/listing/10000001/rental-economics").status_code==404
    assert client.get("/api/listing/not-an-id/rental-economics").status_code==404
    fetch.return_value=sale()
    result=build_rental_economics(sale(),ix(),NetYieldParams())
    monkeypatch.setattr("bot.core.rental_economics.rental_economics_for_listing",AsyncMock(return_value=result))
    response=client.get("/api/listing/10000001/rental-economics")
    assert response.status_code==200 and response.json()["net_yield_pct"]==7.5
    assert "listing" not in response.json()


@pytest.mark.asyncio
async def test_refresh_memoizes_segments_and_optimistically_checks_identity(monkeypatch):
    monkeypatch.setattr("bot.db.pg.fetch",AsyncMock(return_value=[sale(),sale("10000002")]))
    lookup=AsyncMock(return_value=ix()); monkeypatch.setattr("bot.core.rental_parser.lookup_rental_estimate",lookup)
    execute=AsyncMock(side_effect=["UPDATE 1","UPDATE 0"]); monkeypatch.setattr("bot.db.pg.execute",execute)
    result=await refresh.refresh_batch()
    assert result["scanned"]==2 and result["changed"]==1 and result["optimistic_skipped"]==1
    lookup.assert_awaited_once()
    sql=execute.await_args_list[0].args[0]
    assert "complex_name IS NOT DISTINCT FROM" in sql and "house_attribution IS NOT DISTINCT FROM" in sql
    assert "district IS NOT DISTINCT FROM $16" in sql and "market_type IS NOT DISTINCT FROM $17" in sql
    assert "city" not in sql and "deal_score" not in sql
    snapshot=json.loads(execute.await_args_list[0].args[2])
    assert snapshot["rent_source"]["policy_version"]=="3" and snapshot["listing"]["id"]=="10000001"


@pytest.mark.asyncio
async def test_missing_source_clears_all_legacy_derived_values(monkeypatch):
    monkeypatch.setattr("bot.db.pg.fetch",AsyncMock(return_value=[sale()]))
    monkeypatch.setattr("bot.core.rental_parser.lookup_rental_estimate",AsyncMock(return_value=None))
    execute=AsyncMock(return_value="UPDATE 1"); monkeypatch.setattr("bot.db.pg.execute",execute)
    await refresh.refresh_batch()
    assert execute.await_args.args[2:8]==(None,)*6


@pytest.mark.asyncio
async def test_full_refresh_is_bounded_and_exposes_partial_cursor(monkeypatch):
    batch=AsyncMock(side_effect=[{"scanned":2,"changed":2,"optimistic_skipped":0,"has_more":True,"next_after_id":"10000002"},
                                {"scanned":1,"changed":1,"optimistic_skipped":0,"has_more":False,"next_after_id":"10000003"}])
    monkeypatch.setattr(refresh,"refresh_batch",batch)
    result=await refresh.refresh_all(batch_size=2)
    assert result["complete"] and result["scanned"]==3
    assert batch.await_args_list[1].kwargs["after_id"]=="10000002"
    assert batch.await_args_list[0].kwargs["_lookup_cache"] is batch.await_args_list[1].kwargs["_lookup_cache"]


@pytest.mark.parametrize("values",[{"net_yield_min":float("nan")},{"net_yield_min":-1},{"rent_confidence":"certain"},{"sort":"price;DROP"}])
def test_map_rejects_invalid_filters(values):
    with pytest.raises(ValueError): filters.append_rental_filters([],[],**values)


def test_map_default_unchanged_and_active_filters_bind_params():
    conds,params=[],["already-bound"]
    assert filters.append_rental_filters(conds,params) is None and conds==[]
    order=filters.append_rental_filters(conds,params,net_yield_min=7,sort="net_yield_pct",high_yield=True)
    sql=" ".join(conds)
    assert "$2::jsonb" in sql and "$3" in sql and params[2]==7
    assert "policy_version'='3'" in sql and "259200" in sql and "93600" in sql
    assert "district_class" in sql and "WHEN 'district_class' THEN 0" in sql and "p90_net" in sql
    assert "('medium','high')" in sql and "->>'level'='район'" in sql
    assert "DESC NULLS LAST" in order and "a.id" in order
    assert json.loads(params[1])==asdict(NetYieldParams())


def test_unsafe_sql_identifiers_rejected():
    for alias,placeholder in (("a;DROP","$1"),("a","$1;DROP")):
        with pytest.raises(ValueError): filters.qualified_snapshot_sql(alias,placeholder)


def test_narrowest_qualified_benchmark_order():
    assert benchmark_scopes({"district":"Есиль","rental_housing_class":"комфорт"})==[
        ("district_class","Есиль · комфорт"),("district","Есиль"),("class","комфорт"),("city","astana")]
    assert benchmark_scopes({})==[("city","astana")]


def test_static_integration_uses_existing_entry_points():
    from pathlib import Path
    root=Path(__file__).resolve().parents[1]
    assert 'include "_rental_economics.html"' in (root/"bot/templates/analytics_detail.html").read_text(encoding="utf-8")
    dashboard=(root/"bot/templates/dashboard.html").read_text(encoding="utf-8")
    assert "HatuliRentalEconomics?.render(d.rental_economics)" in dashboard and "p90" in dashboard
    assert "dispatcher_loop(bot)" in (root/"service_buyer_bot.py").read_text(encoding="utf-8")
    assert "refresh_all()" in (root/"service_apartments.py").read_text(encoding="utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("failed",[None,"finish","dedup","refresh","benchmarks","score","sheets"])
async def test_sale_cycle_orders_fresh_rent_before_score_and_sheets_with_independent_failures(monkeypatch,failed):
    """Execute the actual tail AST with mocked stages; never run the crawl."""
    import ast
    import copy
    from pathlib import Path
    import sys
    from types import SimpleNamespace
    from unittest.mock import Mock
    source=Path(__file__).resolve().parents[1]/"service_apartments.py"
    cycle=next(node for node in ast.parse(source.read_text(encoding="utf-8")).body
               if isinstance(node,ast.AsyncFunctionDef) and node.name=="run_cycle")
    tail=next(node for node in cycle.body if isinstance(node,ast.Try) and any(
        isinstance(call,ast.Call) and isinstance(call.func,ast.Name) and call.func.id=="sync_apartments_to_sheets_pg"
        for call in ast.walk(node)))
    calls=[]
    def stage(name):
        async def execute(*args,**kwargs):
            calls.append(name)
            if failed==name: raise RuntimeError("Synthetic stage failure")
            return 0 if name=="dedup" else {"complete":True}
        return execute
    for module,functions in {
        "finish_classify":{"apply_finish_classification":"finish"},
        "dedup_listings":{"deduplicate_apartment_listings":"dedup"},
        "rental_economics_refresh":{"refresh_all":"refresh","rebuild_benchmarks":"benchmarks"},
        "deal_score":{"apply_deal_scores":"score"},
    }.items():
        monkeypatch.setitem(sys.modules,"bot.core."+module,SimpleNamespace(**{name:stage(label) for name,label in functions.items()}))
    async def set_setting(key,value): calls.append(key)
    namespace={"app_settings":SimpleNamespace(get_bool=lambda *a:True,get=lambda *a:"",set=set_setting),
               "log":SimpleNamespace(info=Mock(),warning=Mock()),"datetime":datetime,"timezone":timezone,
               "sync_apartments_to_sheets_pg":stage("sheets")}
    function=ast.AsyncFunctionDef(name="post_process",args=ast.arguments(posonlyargs=[],args=[],kwonlyargs=[],kw_defaults=[],defaults=[]),
                                  body=[copy.deepcopy(tail)],decorator_list=[])
    tree=ast.fix_missing_locations(ast.Module(body=[function],type_ignores=[]))
    exec(compile(tree,str(source),"exec"),namespace)
    await namespace["post_process"]()
    expected=["finish","dedup","refresh"]+([] if failed=="refresh" else ["benchmarks"])+["score"]
    if failed!="score": expected.append("DEAL_SCORE_LAST_RUN_AT")
    expected.append("sheets")
    if failed!="sheets": expected.append("SHEETS_APARTMENTS_SYNCED_AT")
    assert calls==expected
    names=[call.func.id for call in ast.walk(cycle) if isinstance(call,ast.Call) and isinstance(call.func,ast.Name)]
    assert names.count("refresh_all")==names.count("apply_deal_scores")==1
