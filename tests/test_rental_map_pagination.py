"""Rental inventory pagination must include the tail after unbound rows."""
from datetime import datetime, timezone
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import httpx
from fastapi import FastAPI
import pytest
import pytest_asyncio

ROOT = Path(__file__).parents[1]


@pytest.fixture(autouse=True)
def isolated_database(monkeypatch):
    async def reject(*args, **kwargs):
        pytest.fail("Pagination tests must never access a real database")
    monkeypatch.setattr("asyncpg.connect", reject)
    monkeypatch.setattr("asyncpg.create_pool", reject)
    for name in ("fetch", "fetchrow", "fetchval", "execute"):
        monkeypatch.setattr("bot.db.pg." + name, reject)


@pytest_asyncio.fixture
async def client(monkeypatch):
    from bot.core import site_auth
    import terminal_extras
    async def tier(request):
        return getattr(request.state, "test_role", "public")
    monkeypatch.setattr(site_auth, "get_user_tier", tier)
    app = FastAPI()
    @app.middleware("http")
    async def auth(request, call_next):
        request.state.test_role = request.headers.get("x-test-role", "public")
        request.state.admin_user = {"id": 1, "username": "test"} if request.state.test_role == "admin" else None
        return await call_next(request)
    app.include_router(terminal_extras.make_extras_router(SimpleNamespace(env=SimpleNamespace(globals={}))))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as connection:
        yield connection


def inventory(count=1001, *, unbound_first=1000):
    now = datetime.now(timezone.utc)
    return [{"id": str(100000 + index), "url": f"https://krisha.kz/a/show/{100000 + index}",
             "price": 250_000, "rooms": 2, "area": 60, "floor": 3, "floors_total": 10,
             "address": "QA", "complex_name": None, "complex_key": "", "district": "",
             "found_at": now, "own_lat": None if index < unbound_first else 51.1,
             "own_lon": None if index < unbound_first else 71.4,
             "photos": None, "photo_url": None, "prev_price": None, "price_changed_at": None,
             "phone": "private-contact", "description": "private-description"}
            for index in range(count)]


def mock_inventory(monkeypatch, rows):
    calls = []
    async def fetch(sql, *params):
        calls.append((sql, params))
        if "SELECT COUNT(*) AS total_matching" in sql:
            return [{"total_matching": len(rows)}]
        if "FROM rental_listings r" in sql:
            take, offset = params[-2:]
            assert "ORDER BY r.found_at DESC, r.id DESC" in sql
            assert "OFFSET $" in sql and "LIMIT $" in sql
            return rows[offset:offset + take]
        return []
    monkeypatch.setattr("bot.db.pg.fetch", fetch)
    return calls


@pytest.mark.asyncio
async def test_more_than_1000_rows_and_entire_unbound_first_page_do_not_hide_tail(client, monkeypatch):
    mock_inventory(monkeypatch, inventory())
    first = await client.get("/admin/api/map-points?type=rental&limit=1000", headers={"x-test-role": "admin"})
    assert first.status_code == 200 and first.headers["cache-control"] == "no-store"
    data = first.json()
    assert data["points"] == [] and data["no_geo"] == 1000
    assert data["batch_rows"] == 1000 and data["total_matching"] == 1001
    assert data["has_more"] and data["next_offset"] == 1000
    second = await client.get("/admin/api/map-points?type=rental&limit=1000&offset=1000", headers={"x-test-role": "admin"})
    tail = second.json()
    assert [point["id"] for point in tail["points"]] == ["101000"]
    assert tail["batch_rows"] == 1 and tail["total_matching"] == 1001
    assert not tail["has_more"] and tail["next_offset"] is None
    assert tail["geo_scope"] == "city" and tail["market_coverage"] == "unknown"
    assert "private-contact" not in second.text and "private-description" not in second.text


@pytest.mark.asyncio
@pytest.mark.parametrize("offset,limit,want_offset,want_limit", [(-9, 0, 0, 1), (0, 99999, 0, 1000), (1001, 300, 1001, 300)])
async def test_batch_limits_and_out_of_range_offset_return_consistent_totals(client, monkeypatch, offset, limit, want_offset, want_limit):
    calls = mock_inventory(monkeypatch, inventory())
    response = await client.get(f"/admin/api/map-points?type=rental&offset={offset}&limit={limit}", headers={"x-test-role": "admin"})
    data = response.json()
    assert data["offset"] == want_offset and data["limit"] == want_limit
    assert data["total_matching"] == 1001
    row_query = next(params for sql, params in calls if "FROM rental_listings r" in sql)
    assert row_query[-2:] == (want_limit + 1, want_offset)
    if want_offset == 1001:
        assert not data["has_more"] and data["batch_rows"] == 0


@pytest.mark.asyncio
async def test_total_and_page_share_filters_clean_scope_and_do_not_claim_bbox_precision(client, monkeypatch):
    calls = mock_inventory(monkeypatch, inventory(2, unbound_first=0))
    response = await client.get("/admin/api/map-points?type=rental&rooms=2,3&price_min=200000&price_max=300000&area_min=50&area_max=70&min_lat=51&max_lat=52&min_lon=71&max_lon=72", headers={"x-test-role": "admin"})
    count_sql, count_args = next(call for call in calls if "SELECT COUNT(*) AS total_matching" in call[0])
    row_sql, row_args = next(call for call in calls if "FROM rental_listings r" in call[0])
    assert row_args[:-2] == count_args
    for sql in (count_sql, row_sql):
        for clause in ("r.rooms = ANY", "r.price >=", "r.price <=", "r.area >=", "r.area <=",
                       "is_active IS TRUE", "archived_at IS NULL", "interval '72 hours'", "dup_needs_review", "duplicate_of"):
            assert clause in sql
    assert response.json()["geo_scope"] == "city"


@pytest.mark.asyncio
async def test_public_tier_cannot_enumerate_private_rental_pages(client, monkeypatch):
    response = await client.get("/admin/api/map-points?type=rental&offset=300&limit=300")
    assert response.status_code == 200
    assert response.json()["points"] == [] and response.json()["total_matching"] == 0
    assert not response.json()["has_more"]
    assert response.headers["cache-control"] == "no-store"


def test_real_template_loader_handles_unbound_tail_duplicates_and_filter_changes():
    node = shutil.which("node")
    assert node, "Node.js is required to verify the production pagination loader"
    result = subprocess.run([node, str(ROOT / "tests/rental_map_loader.test.cjs")],
                            cwd=ROOT, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr or result.stdout
