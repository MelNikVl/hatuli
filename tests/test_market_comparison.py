"""Isolated behavioral tests; no pool, migrations, live DB or network."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import json
import sys

import pytest

from bot.core.hexgrid import hex_center
from bot.core.market_comparison import (
    AREA_BAND, build_market_snapshot, compare_market, hex_distance,
    load_market_snapshot, public_primary_offer, publish_market_snapshot,
)
from scripts.publish_market_comparison import read_market_rows

NOW = datetime(2026, 10, 13, 5, tzinfo=timezone.utc)


def primary(uid=1, hid="0:0", **extra):
    lat, lon = hex_center(hid)
    return {"id": uid, "complex_id": 10, "complex_name": "Пример", "source": "bi_group", "status": "available",
            "rooms": 2, "area": 60, "price": 30_000_000, "floor": 3, "floors_total": 10,
            "lat": lat, "lon": lon, "last_seen_at": NOW-timedelta(hours=1), "build_status": "Строящийся", **extra}


def secondary(uid=100000, hid="0:0", **extra):
    lat, lon = hex_center(hid)
    return {"id": str(uid), "url": f"https://krisha.kz/a/show/{uid}", "complex_id": 20, "complex_name": "Другой пример",
            "rooms": 2, "area": 60, "price": 36_000_000, "floor": 3, "floors_total": 10,
            "lat": lat, "lon": lon, "last_seen": NOW-timedelta(hours=1), "market_type": "secondary",
            "is_active": True, "is_duplicate": False, "dup_needs_review": False, **extra}


def snapshot(primary_count=5, secondary_count=5, **kwargs):
    return build_market_snapshot([primary(i+1) for i in range(primary_count)], [secondary(i+100000) for i in range(secondary_count)], now=NOW, **kwargs)


def test_matched_prices_rooms_area_and_denominator():
    data = build_market_snapshot([primary(i+1) for i in range(5)] + [primary(99, rooms=3, price=10_000_000), primary(98, area=100)],
                                 [secondary(i+100000) for i in range(5)] + [secondary(900000, area=100)], now=NOW)
    result = compare_market(data, "0:0", rooms=2, area=60, now=NOW)
    assert result["stats"]["primary"]["count"] == result["stats"]["secondary"]["count"] == 5
    assert result["stats"]["primary"]["median_per_m2"] == 500000
    assert result["stats"]["secondary"]["median_per_m2"] == 600000
    assert result["delta_pct"] == 16.7
    assert result["area_min"] == 51 and result["area_max"] == 69
    assert "не сравнение полной стоимости" in result["stats"]["primary"]["advantages"][0]


def test_rings_include_center_and_monotonic_growth():
    ps = [primary(1, "0:0"), primary(2, "1:0"), primary(3, "2:0"), primary(4, "4:0"), primary(5, "8:0"), primary(6, "9:0")]
    ss = [secondary(100000+i, str(i)+":0") for i in range(10)]
    result = compare_market(build_market_snapshot(ps, ss, now=NOW), "0:0", now=NOW)
    assert [t["cells_count"] for t in result["tiers"][:5]] == [1, 7, 19, 61, 217]
    assert [t["primary_count"] for t in result["tiers"]] == [1, 2, 3, 4, 5, 6]
    assert [t["secondary_count"] for t in result["tiers"]] == [1, 2, 3, 5, 9, 10]
    assert hex_distance("-2:3", "1:1") == 3


@pytest.mark.parametrize("kind,field,value", [
    ("primary", "status", "reserved"), ("primary", "status", "sold"),
    ("primary", "source", "krisha"), ("primary", "last_seen_at", NOW-timedelta(hours=73)),
    ("primary", "last_seen_at", NOW+timedelta(seconds=1)),
    ("secondary", "is_active", False), ("secondary", "is_active", None),
    ("secondary", "market_type", "primary"), ("secondary", "is_duplicate", True),
    ("secondary", "dup_needs_review", True), ("secondary", "linked_primary_unit", True),
    ("secondary", "year_built", 2027), ("secondary", "url", "https://krisha.kz/a/show/100000?admin_token=private"),
    ("secondary", "lat", 48), ("secondary", "area", 0), ("secondary", "price", float("nan")),
    ("secondary", "floor", 11),
])
def test_excludes_unconfirmed_unavailable_invalid_and_stale(kind, field, value):
    ps = [primary(**{field: value})] if kind == "primary" else []
    ss = [secondary(**{field: value})] if kind == "secondary" else []
    assert not build_market_snapshot(ps, ss, now=NOW)["offers"]


def test_no_private_projection_or_primary_double_count():
    secret = {"address": "private apartment address", "phone": "+700000000", "raw_json": {"contact": "private"}, "description": "owner info"}
    data = build_market_snapshot([primary(**secret)], [secondary(property_id=77, **secret), secondary(100001, property_id=77)], now=NOW,
                                 coverage={"krisha_primary_recent": 1064, "secret_address": 123})
    serialized = json.dumps(data)
    assert all(s not in serialized for s in ("private", "address", "phone", "raw_json", "property_id", '"lat"', '"lon"'))
    assert data["summary"]["primary_count"] == 1
    assert data["summary"]["secondary_count"] == 1
    assert data["summary"]["coverage"] == {"krisha_primary_recent": 1064}


def test_completion_year_does_not_prove_commission_or_finish():
    data = build_market_snapshot([primary(build_status=None, completion_year=2020, finish_level="furnished")], [secondary()], now=NOW)
    offer = public_primary_offer(data, 1)
    assert offer["build_status"] == "unknown"
    assert offer["finish_level"] is None
    assert "не подтверждён" in offer["build_status_label"]
    assert public_primary_offer(data, "123invalid") is None


def test_minimum_evidence_and_dispersion_hide_price_claim():
    assert compare_market(snapshot(4, 20), "0:0", now=NOW)["delta_pct"] is None
    varying = [primary(i+1, price=price) for i, price in enumerate((10_000_000, 20_000_000, 30_000_000, 60_000_000, 100_000_000))]
    result = compare_market(build_market_snapshot(varying, [secondary(i+100000) for i in range(5)], now=NOW), "0:0", now=NOW)
    assert result["delta_pct"] is None
    assert "разброс" in result["confidence_reason"]


def test_stale_snapshot_keeps_date_and_hides_claims():
    data = snapshot()
    result = compare_market(data, "0:0", now=NOW+timedelta(hours=27))
    assert result["stale"] and result["delta_pct"] is None
    assert result["updated_at_label"] == "13.10.2026, 10:00"
    assert "устарел" in result["notices"][0]


def test_city_has_explicit_mix_caution_and_rough_finish_is_not_ready():
    data = build_market_snapshot([primary()], [secondary(finish_level="rough")], now=NOW)
    result = compare_market(data, scope="city", now=NOW)
    assert not any("готовой отделкой" in a for a in result["stats"]["secondary"]["advantages"])
    assert any("смешивает" in c for c in result["stats"]["secondary"]["cautions"])
    assert any("ремонта" in c for c in result["stats"]["secondary"]["cautions"])


def test_atomic_projection_cache_and_bad_refresh_preserve_previous(tmp_path):
    path = tmp_path / "comparison.json"
    data = snapshot()
    data["offers"][0]["secret"] = "must not persist"
    publish_market_snapshot(data, path)
    assert "must not persist" not in path.read_text(encoding="utf-8")
    assert load_market_snapshot(path)["summary"]["primary_count"] == 5
    previous = path.read_bytes()
    with pytest.raises(ValueError):
        publish_market_snapshot(snapshot(0, 5), path)
    assert path.read_bytes() == previous
    changed = snapshot(6, 5)
    publish_market_snapshot(changed, path)
    assert load_market_snapshot(path)["summary"]["primary_count"] == 6
    assert not list(tmp_path.glob("*.tmp"))


def test_missing_and_malicious_snapshot_return_empty(tmp_path):
    path = tmp_path / "comparison.json"
    assert not load_market_snapshot(path)["offers"]
    data = snapshot()
    data["offers"][0]["url"] = "https://evil.example/admin"
    path.write_text(json.dumps(data), encoding="utf-8")
    assert not load_market_snapshot(path)["offers"]


def test_actual_scanner_edge_controls_membership():
    # Keep away from the exact boundary to avoid projection floating-point ties.
    lat0, lon0 = hex_center("0:0")
    lat1, lon1 = hex_center("1:0")
    coords = {"lat": lat0+.8*(lat1-lat0), "lon": lon0+.8*(lon1-lon0)}
    data = build_market_snapshot([primary(**coords)], [secondary(**coords)], now=NOW, edge_m=100)
    assert data["edge_m"] == 100
    assert all(o["hex_id"] == "0:0" for o in data["offers"])
    assert all(o["hex_id"] == "1:0" for o in build_market_snapshot([primary(**coords)], [secondary(**coords)], now=NOW, edge_m=50)["offers"])


def test_invalid_user_parameters_have_safe_defaults():
    result = compare_market(snapshot(), hex_id="evil<script>", rooms=True, area=float("inf"), scope="invalid", now=NOW)
    assert result["rooms"] == 2 and result["area"] == 60 and result["scope"] == "hex"
    assert result["selected_hex"] == "0:0"


@pytest.mark.asyncio
async def test_publisher_only_reads_in_enforced_readonly_transaction(monkeypatch):
    class Transaction:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    connection = SimpleNamespace(fetch=AsyncMock(side_effect=[[primary()], [secondary()]]),
                                 fetchrow=AsyncMock(return_value={"krisha_primary_recent": 1}),
                                 fetchval=AsyncMock(return_value="100"), close=AsyncMock())
    def transaction(**kwargs):
        assert kwargs == {"isolation": "repeatable_read", "readonly": True}
        return Transaction()
    connection.transaction = transaction
    connect = AsyncMock(return_value=connection)
    monkeypatch.setitem(sys.modules, "asyncpg", SimpleNamespace(connect=connect))
    ps, ss, edge, coverage = await read_market_rows("postgresql://unused:unused@127.0.0.1:1/never_connect", NOW)
    assert edge == 100 and len(ps) == len(ss) == 1
    assert connect.call_args.kwargs["server_settings"]["default_transaction_read_only"] == "on"
    for call in connection.fetch.call_args_list:
        assert "SELECT" in call.args[0] and "UPDATE " not in call.args[0] and "INSERT " not in call.args[0]
    connection.close.assert_awaited_once()
