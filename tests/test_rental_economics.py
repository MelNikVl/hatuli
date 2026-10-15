"""Qualified asking-rent scenarios; no source HTTP or live database."""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from bot.core import rental_economics as eco
from bot.core.rental_yield import NetYieldParams

NOW = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused:unused@127.0.0.1:1/rental_stage1_test")
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    denied = AsyncMock(side_effect=AssertionError("No database connection or migrations"))
    monkeypatch.setattr("asyncpg.connect", denied)
    monkeypatch.setattr("asyncpg.create_pool", denied)
    monkeypatch.setattr("bot.db.pg.init_pool", denied)
    monkeypatch.setattr("bot.db.settings.get_int", lambda key, default: default)


def listing(**extra):
    return {"id": "10000001", "price": 40000000, "area": 60.0, "rooms": 2,
            "complex_id": 10, "complex_name": "ЖК", "complex_resolution": "url", "resolved_house_id": None,
            "house_attribution": None, "district": "Есиль", "is_active": True, "is_duplicate": False,
            "dup_needs_review": False, "archived_at": None, "duplicate_of": None,
            "first_seen": NOW-timedelta(days=3), "last_seen": NOW-timedelta(hours=1), **extra}


def index(**extra):
    return {"level": "ЖК", "median_price": 280000, "avg_price": 280000, "p25_price": 250000,
            "p75_price": 310000, "price_per_sqm": 5000, "sample_count": 10,
            "updated_at": NOW-timedelta(minutes=10), "source_oldest_at": NOW-timedelta(hours=4),
            "source_newest_at": NOW-timedelta(hours=1), "policy_version": "3", "window_days": 30,
            "rent_period": "monthly", **extra}


def benchmark(**extra):
    return {"scope": "district", "scope_value": "Есиль", "rooms": 2, "median_net": 7.0,
            "p90_net": 9.1, "n": 10, "net_values": [5, 5.5, 6, 6.5, 7, 7, 7.5, 8, 9, 10],
            "computed_at": NOW-timedelta(minutes=1), "source_oldest_at": NOW-timedelta(hours=5),
            "source_newest_at": NOW-timedelta(hours=1), "params_used": asdict(NetYieldParams()),
            "policy_version": "3", **extra}


def ready(row=None, ix=None, **extra):
    return eco.build_rental_economics(row or listing(), ix or index(), NetYieldParams(), now=NOW, **extra)


def test_quartile_range_scaled_from_same_index_and_net_is_primary_payback():
    result = ready()
    assert result["est_rent"] == 300000 and result["rent_range"] == [267857, 332143]
    assert result["gross_yield_pct"] == 9.0 and result["net_yield_pct"] == 7.5
    assert result["payback_years"] == 13.3 and result["gross_payback_years"] == 11.1
    assert result["confidence"] == "high" and result["asking_prices"] and result["scenario"]
    assert result["params_used"] == asdict(NetYieldParams())
    assert "tax" not in result and "occupancy_forecast" not in result


@pytest.mark.parametrize("level,confidence", [("ЖК", "high"), ("район", "medium"), ("город", "low"),
    ("район (все комн.)", "low"), ("город (все комн.)", "low")])
def test_confidence_follows_actual_level_and_room_scope(level, confidence):
    assert ready(ix=index(level=level))["confidence"] == confidence


@pytest.mark.parametrize("change", [
    {"rent_period": "daily"}, {"rent_period": "unknown"}, {"policy_version": "2"}, {"window_days": None},
    {"sample_count": 4}, {"sample_count": True}, {"source_oldest_at": NOW-timedelta(hours=73)},
    {"source_newest_at": NOW+timedelta(seconds=1)}, {"updated_at": NOW+timedelta(seconds=1)},
    {"updated_at": None}, {"source_oldest_at": None}, {"p25_price": 300000},
    {"p75_price": 200000}, {"level": "invented"},
])
def test_unqualified_index_never_produces_economics(change):
    assert ready(ix=index(**change)) is None


@pytest.mark.parametrize("change", [{"is_active": False}, {"is_duplicate": True}, {"dup_needs_review": True},
    {"duplicate_of": "other"}, {"archived_at": NOW}, {"last_seen": NOW-timedelta(hours=73)},
    {"last_seen": None}, {"last_seen": NOW+timedelta(seconds=1)}, {"area": float("nan")},
    {"price": 0}, {"rooms": True}])
def test_ineligible_sale_hides_whole_block(change):
    assert ready(row=listing(**change)) is None


def test_area_and_city_minimum_ten_is_not_complex_minimum_five():
    assert ready(ix=index(sample_count=5)) is not None
    assert ready(ix=index(level="район", sample_count=9)) is None
    assert ready(ix=index(level="город", sample_count=9)) is None


def test_high_yield_uses_qualified_p90_and_medium_minimum_not_fixed_gross():
    result = ready(row=listing(price=25000000), benchmark=benchmark())
    assert result["high_yield"] is True and result["net_yield_pct"] == 12.0
    assert result["benchmark"]["percentile"] == 100
    assert ready(row=listing(price=25000000))["high_yield"] is False
    assert ready(row=listing(price=25000000), ix=index(level="город"), benchmark=benchmark())["high_yield"] is False
    assert ready(benchmark=benchmark())["high_yield"] is False


@pytest.mark.parametrize("change", [{"computed_at": NOW-timedelta(hours=27)}, {"source_oldest_at": NOW-timedelta(hours=73)},
    {"n": 9}, {"policy_version": "2"}, {"params_used": {"occupancy_share": 1}}, {"net_values": [10]}])
def test_bad_benchmark_is_hidden_without_hiding_valid_rent(change):
    result = ready(benchmark=benchmark(**change))
    assert result is not None and "benchmark" not in result and result["high_yield"] is False


def test_cached_snapshot_revalidates_facts_params_source_and_arithmetic():
    row = listing()
    row["rental_economics"] = ready(benchmark=benchmark())
    assert eco.cached_rental_economics(row, now=NOW, params=NetYieldParams()) is not None
    assert eco.cached_rental_economics({**row, "price": 39000000}, now=NOW) is None
    assert eco.cached_rental_economics({**row, "complex_id": 99}, now=NOW) is None
    assert eco.cached_rental_economics({**row, "district": "Алматы"}, now=NOW) is None
    assert eco.cached_rental_economics({**row, "market_type": "primary"}, now=NOW) is None
    assert eco.cached_rental_economics(row, now=NOW+timedelta(hours=70)) is None
    assert eco.cached_rental_economics(row, now=NOW, params=NetYieldParams(.9, .1, 1.02)) is None
    bad = deepcopy(row)
    bad["rental_economics"]["net_yield_pct"] = 20
    assert eco.cached_rental_economics(bad, now=NOW) is None


def test_unfinished_primary_has_no_current_rental_income_block():
    assert ready(row=listing(market_type="primary")) is None


def test_cached_snapshot_drops_unknown_private_fields_and_untrusted_comps():
    row = listing()
    row["rental_economics"] = {**ready(), "contact": "PRIVATE", "rental_comps": [{"phone": "PRIVATE"}]}
    result = eco.cached_rental_economics(row, now=NOW)
    assert "contact" not in result and result["rental_comps"] == []


def test_benchmarks_require_ten_independent_qualified_lives_of_two_days():
    rows = []
    for i in range(10):
        row = listing(id=str(10000001+i), property_id=i+1, rental_housing_class="комфорт")
        row["rental_economics"] = ready(row=row)
        rows.append(row)
    result = eco.build_benchmarks(rows, params=NetYieldParams(), now=NOW)
    assert {item["scope"] for item in result} == {"city", "district", "class", "district_class"}
    assert all(item["n"] == 10 and item["median_net"] == 7.5 and item["p90_net"] == 7.5 for item in result)
    assert eco.build_benchmarks(rows[:9], params=NetYieldParams(), now=NOW) == []
    assert eco.build_benchmarks([{**row, "first_seen": NOW-timedelta(days=1)} for row in rows], params=NetYieldParams(), now=NOW) == []
    assert eco.build_benchmarks([{**row, "property_id": 1} for row in rows], params=NetYieldParams(), now=NOW) == []


@pytest.mark.asyncio
async def test_runtime_failure_hides_block_without_fabricating_values(monkeypatch):
    monkeypatch.setattr("bot.core.rental_parser.lookup_rental_estimate", AsyncMock(side_effect=RuntimeError("PRIVATE-DSN")))
    assert await eco.rental_economics_for_listing(listing(), now=NOW) is None


@pytest.mark.asyncio
async def test_comps_keep_source_address_without_flat_or_contact(monkeypatch):
    now=datetime.now(timezone.utc)
    row={"id":"10000002","price":300000,"area":60,"rooms":2,"last_seen":now-timedelta(hours=1),
         "is_active":True,"rent_period":"monthly","complex_binding_verified":True,
         "address":"Анет баба 4, кв. 25 — Телефон +77001234567"}
    fetch=AsyncMock(return_value=[row]);monkeypatch.setattr("bot.db.pg.fetch",fetch)
    result=await eco.rental_comps_for_listing(listing())
    assert result[0]["address"]=="Анет баба 4"
    assert "r.address" in fetch.await_args.args[0] and "77001234567" not in str(result)
