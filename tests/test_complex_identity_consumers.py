"""HTTP and membership regressions for canonical rental/catalog/map consumers."""
import json
import os
import re
from uuid import uuid4

import pytest
import pytest_asyncio
from tests.admin_auth_helpers import admin_cookies


@pytest_asyncio.fixture
async def client(tmp_path):
    import httpx
    from bot.db.pg import init_pool, close_pool
    from bot.db.compat import BotDB
    from bot.admin_web import create_admin_app
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        pytest.skip("Set DATABASE_URL to an isolated test database")
    await init_pool(dsn)
    sqlite_path = str(tmp_path / "consumer-test.db")
    db = BotDB(sqlite_path)
    await db.init()
    app = create_admin_app(db, "test", "test", sqlite_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                cookies=await admin_cookies()) as test_client:
        yield test_client
    await close_pool()


@pytest_asyncio.fixture
async def identity(client):
    from bot.db.pg import execute, fetchval
    prefix = "__test_cxconsumer_" + uuid4().hex + "__"
    root = await fetchval("INSERT INTO complexes(name,is_umbrella,lat,lon) VALUES($1,TRUE,51.1,71.4) RETURNING id", prefix + "root")
    child = await fetchval("INSERT INTO complexes(name,parent_complex_id,lat,lon) VALUES($1,$2,51.1,71.4) RETURNING id", prefix + "child", root)
    alias = await fetchval("INSERT INTO complexes(name,canonical_id,canonical_reason,lat,lon) "
                           "VALUES($1,$2,'krisha_slug',51.1,71.4) RETURNING id", prefix + "child ", child)
    other = await fetchval("INSERT INTO complexes(name,lat,lon) VALUES($1,51.2,71.5) RETURNING id", prefix + "other")
    ids = [root, child, alias, other]
    try:
        yield {"prefix": prefix, "root": root, "child": child, "alias": alias, "other": other, "ids": ids}
    finally:
        await execute("DELETE FROM unit_source_links WHERE unit_id IN "
                      "(SELECT id FROM newbuild_units WHERE complex_id=ANY($1::int[]))", ids)
        await execute("DELETE FROM newbuild_unit_price_history WHERE unit_id IN "
                      "(SELECT id FROM newbuild_units WHERE complex_id=ANY($1::int[]))", ids)
        await execute("DELETE FROM newbuild_units WHERE complex_id=ANY($1::int[])", ids)
        await execute("DELETE FROM complex_aliases WHERE complex_id=ANY($1::int[])", ids)
        await execute("DELETE FROM complex_addresses WHERE complex_id=ANY($1::int[])", ids)
        await execute("DELETE FROM rental_listings WHERE id LIKE $1", prefix + "%")
        await execute("DELETE FROM apartment_listings WHERE id LIKE $1", prefix + "%")
        await execute("DELETE FROM complexes WHERE id=ANY($1::int[])", ids)
        await execute("DELETE FROM developers WHERE name LIKE $1", prefix + "%")


async def _alias(cid, name, status="verified"):
    from bot.db.pg import execute
    await execute("INSERT INTO complex_aliases(complex_id,name,normalized_name,source,source_id,status) "
                  "VALUES($1,$2,complex_name_key($2),'consumer-test',$2,$3)", cid, name, status)


async def _sale(identity, suffix, cid, name=None, *, active=True):
    from bot.db.pg import execute
    lid = identity["prefix"] + suffix
    await execute("INSERT INTO apartment_listings(id,url,complex_id,complex_name,price,area,rooms,lat,lon,is_active,first_seen,last_seen,archived_at) "
                  "VALUES($1,$1,$2,$3,25000000,50,2,51.1,71.4,$4,now()-interval '10 days',now(),"
                  "CASE WHEN $4 THEN NULL ELSE now()-interval '1 day' END)",
                  lid, cid, name or identity["prefix"] + "stale-text", active)
    return lid


@pytest.mark.asyncio
async def test_registry_catalog_search_and_name_redirect_are_verified_and_unambiguous(client, identity):
    p = identity["prefix"]
    reviewed = p + "translated-name"
    observed = p + "unreviewed-name"
    await _alias(identity["child"], reviewed)
    await _alias(identity["child"], observed, "observed")
    result = await client.get("/complexes", params={"search": reviewed})
    assert result.status_code == 200 and f"/complex/{identity['child']}" in result.text
    result = await client.get("/complexes", params={"search": observed})
    assert result.status_code == 200 and f"/complex/{identity['child']}" not in result.text
    result = await client.get("/complex/find", params={"name": 'ЖК «' + reviewed + '»'})
    assert result.headers["location"] == f"/admin/complex/{identity['child']}"
    await _alias(identity["other"], reviewed)
    result = await client.get("/complex/find", params={"name": reviewed})
    assert result.status_code == 302 and result.headers["location"].startswith("/admin/complexes?search=")


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["price-dynamics", "turnover-dynamics"])
async def test_rental_graphs_follow_aliases_umbrella_and_room_filters(client, identity, endpoint):
    from bot.db.pg import execute
    p = identity["prefix"]
    reviewed = p + "rental-reviewed"
    await _alias(identity["child"], reviewed)
    for index, name in enumerate([p + "child", reviewed, p + "root"]):
        await execute("INSERT INTO rental_listings(id,complex_name,rooms,price,area,found_at,last_seen) "
                      "VALUES($1,$2,2,200000,50,now()-interval '10 days',now()-interval '4 days')",
                      p + str(index), name)
    for key, expected in [("root", 3), ("child", 2), ("alias", 2)]:
        path = f"/admin/api/complex/{identity[key]}/{endpoint}"
        result = await client.get(path, params={"kind": "rental", "rooms": "2", "days": 90})
        assert result.status_code == 200
        points = result.json()["data"]["2"]
        assert sum(point["n"] for point in points) == expected
        metric = "median_price" if endpoint == "price-dynamics" else "avg_days"
        assert all(point[metric] == (200000 if endpoint == "price-dynamics" else 6) for point in points)
        empty = await client.get(path, params={"kind": "rental", "rooms": "1", "days": 90})
        assert empty.status_code == 200 and empty.json()["data"] == {}


@pytest.mark.asyncio
async def test_same_named_sibling_aliases_remain_unassigned_even_on_umbrella(client, identity):
    from bot.db.pg import execute, fetchval
    from bot.core.complex_membership import listing_complex_match_sql, rental_complex_match_sql
    p = identity["prefix"]
    await execute("UPDATE complexes SET parent_complex_id=$2 WHERE id=$1", identity["other"], identity["root"])
    shared = p + "shared-siblings"
    await _alias(identity["child"], shared)
    await _alias(identity["other"], shared)
    await _sale(identity, "unassigned", None, shared)
    await execute("INSERT INTO rental_listings(id,complex_name,rooms,price) VALUES($1,$2,2,200000)", p + "rental", shared)
    assert await fetchval("SELECT count(*) FROM apartment_listings WHERE " +
                          listing_complex_match_sql(include_children=True), identity["root"]) == 0
    assert await fetchval("SELECT count(*) FROM rental_listings WHERE " +
                          rental_complex_match_sql(include_children=True), identity["root"]) == 0


@pytest.mark.asyncio
async def test_sale_map_uses_explicit_id_and_does_not_multiply_name_aliases(client, identity):
    from bot.db.pg import execute, fetchval
    p = identity["prefix"]
    dev_a = await fetchval("INSERT INTO developers(name) VALUES($1) RETURNING id", p + "dev-a")
    dev_b = await fetchval("INSERT INTO developers(name) VALUES($1) RETURNING id", p + "dev-b")
    await execute("UPDATE complexes SET developer_id=$2,photos=$3::jsonb WHERE id=$1",
                  identity["child"], dev_a, json.dumps(["https://example.com/a.jpg"]))
    await execute("UPDATE complexes SET developer_id=$2,photos=$3::jsonb WHERE id=$1",
                  identity["other"], dev_b, json.dumps(["https://example.com/b.jpg"]))
    first = await _sale(identity, "bound-child", identity["child"], p + "child")
    second = await _sale(identity, "stale-conflict", identity["other"], p + "child")
    result = await client.get("/admin/api/map-points", params={"rooms": "2"})
    assert result.status_code == 200
    own = [row for row in result.json()["points"] if row["id"] in {first, second}]
    assert len(own) == 2
    own = {row["id"]: row for row in own}
    assert own[first]["developer_id"] == dev_a
    assert own[second]["developer_id"] == dev_b
    assert own[second]["complex_photos"] == ["https://example.com/b.jpg"]


@pytest.mark.asyncio
async def test_catalog_map_metrics_aggregate_ids_without_text_conflicts_and_audit_counts_once(client, identity):
    from bot.db.pg import execute
    p = identity["prefix"]
    await _sale(identity, "active1", identity["child"])
    await _sale(identity, "active2", identity["alias"])
    await _sale(identity, "archived", identity["child"], active=False)
    await execute("UPDATE apartment_listings SET score_total=80 WHERE id LIKE $1", p + "%")
    result = await client.get("/admin/api/complexes-map")
    assert result.status_code == 200
    own = {row["id"]: row for row in result.json()["complexes"]}
    assert own[identity["child"]]["days_to_sell"] == 9
    assert own[identity["root"]]["days_to_sell"] == 9
    result = await client.get("/admin/complexes/data-audit")
    assert result.status_code == 200
    assert re.search(rf'/complex/{identity["child"]}[^<]*.*?</a></td>\s*<td>2</td>', result.text, re.DOTALL)


@pytest.mark.asyncio
async def test_canonical_unit_pages_keep_alias_inventory_and_child_scope(client, identity):
    from bot.db.pg import execute, fetchval
    p = identity["prefix"]
    dev = await fetchval("INSERT INTO developers(name) VALUES($1) RETURNING id", p + "unit-dev")
    await execute("UPDATE complexes SET developer_id=$2,is_newbuild=TRUE WHERE id=ANY($1::int[])",
                  [identity["root"], identity["child"], identity["other"]], dev)
    units = {}
    for key, rooms in [("root", 3), ("alias", 2), ("other", 1)]:
        units[key] = await fetchval("INSERT INTO newbuild_units(complex_id,source,source_unit_id,rooms,price,area,status) "
                                   "VALUES($1,$2,$3,$4,30000000,60,'available') RETURNING id",
                                   identity[key], p + "unit-source", p + key, rooms)
    for key, expected in [("root", {units["root"], units["alias"]}),
                          ("child", {units["alias"]}), ("alias", {units["alias"]})]:
        result = await client.get(f"/admin/api/newbuild-complex/{identity[key]}/units")
        assert result.status_code == 200
        body = result.json()
        assert {item["id"] for item in body["units"] if item["source"] == "developer"} == expected
        assert body["id"] == (identity["child"] if key == "alias" else identity[key])
    filtered = await client.get(f"/admin/api/newbuild-complex/{identity['root']}/units", params={"rooms": "2"})
    assert {item["id"] for item in filtered.json()["units"]} == {units["alias"]}
    page = await client.get(f"/complex/{identity['child']}")
    assert page.status_code == 200 and f"/listing/nb-{units['alias']}" in page.text
    assert f"/listing/nb-{units['root']}" not in page.text
    assert await fetchval("SELECT complex_id FROM newbuild_units WHERE id=$1", units["alias"]) == identity["alias"]


@pytest.mark.asyncio
async def test_quarantined_unique_name_cannot_reenter_cards_or_current_metrics(client, identity):
    from bot.db.pg import execute, fetchval
    from bot.core.complex_membership import listing_complex_match_sql
    from bot.core.complex_metrics import CURRENT_METRICS_SQL
    lid = await _sale(identity, "quarantined", None, identity["prefix"] + "child")
    await execute("UPDATE apartment_listings SET complex_resolution='unbound' WHERE id=$1", lid)
    assert await fetchval("SELECT count(*) FROM apartment_listings WHERE " + listing_complex_match_sql(),
                          identity["child"]) == 0
    await execute(CURRENT_METRICS_SQL)
    assert await fetchval("SELECT listings_count FROM complexes WHERE id=$1", identity["child"]) == 0
    page = await client.get(f"/complex/{identity['child']}")
    assert page.status_code == 200 and f"/listing/{lid}" not in page.text
