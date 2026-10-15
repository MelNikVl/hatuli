"""Listing catalog seeding shares import identity and retains raw evidence."""
import asyncio
import os
from uuid import uuid4

import pytest
import pytest_asyncio

from bot.core.listing_complex_ingest import SOURCE, seed_listing_complexes


@pytest_asyncio.fixture
async def db(monkeypatch):
    from bot.db import pg
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        pytest.skip("Set DATABASE_URL to an isolated test database")
    await pg.init_pool(dsn)
    state = {"prefix": "__test_lcingest_" + uuid4().hex, "listings": [], "developers": []}

    async def no_geocode(*args):
        pass
    monkeypatch.setattr("newbuild_common.geocode_complex", no_geocode)
    try:
        yield state
    finally:
        ids = [r["id"] for r in await pg.fetch(
            "SELECT id FROM complexes WHERE position(lower($1) in lower(name)) > 0", state["prefix"])]
        await pg.execute("DELETE FROM apartment_listings WHERE id=ANY($1::text[])", state["listings"])
        await pg.execute("DELETE FROM complex_aliases WHERE complex_id=ANY($1::int[])", ids)
        await pg.execute("DELETE FROM complex_addresses WHERE complex_id=ANY($1::int[])", ids)
        await pg.execute("DELETE FROM complex_source_links WHERE complex_id=ANY($1::int[])", ids)
        await pg.execute("DELETE FROM complexes WHERE id=ANY($1::int[])", ids)
        await pg.execute("DELETE FROM developers WHERE id=ANY($1::int[])", state["developers"])
        await pg.close_pool()


async def _listing(state, name, address, *, cid=None):
    from bot.db import pg
    lid = state["prefix"] + "listing_" + str(len(state["listings"]))
    state["listings"].append(lid)
    await pg.execute("INSERT INTO apartment_listings (id,url,complex_name,address,complex_id,is_active) "
                     "VALUES ($1,$1,$2,$3,$4,TRUE)", lid, name, address, cid)
    return lid


async def _complex(state, name, *, garbage=False):
    from bot.db import pg
    return await pg.fetchval("INSERT INTO complexes (name,is_garbage) VALUES ($1,$2) RETURNING id",
                             state["prefix"] + name, garbage)


@pytest.mark.asyncio
async def test_normalized_variants_create_one_catalog_row_and_retain_addresses(db):
    from bot.db import pg
    base = db["prefix"] + "Sun"
    a = await _listing(db, base, "Анет баба, 4")
    b = await _listing(db, ' ЖК «' + base.upper() + '» ', "Туркестан, 4Б")
    report = await seed_listing_complexes(listing_ids=[a, b])
    assert report["created"] == 1 and report["observed"] == 2
    cid = await pg.fetchval("SELECT id FROM complexes WHERE complex_name_key(name)=complex_name_key($1)", base)
    assert await pg.fetchval("SELECT count(*) FROM complex_aliases WHERE complex_id=$1 AND source=$2", cid, SOURCE) == 2
    assert await pg.fetchval("SELECT count(*) FROM complex_addresses WHERE complex_id=$1 AND source=$2", cid, SOURCE) == 2
    assert await pg.fetchval("SELECT count(*) FROM apartment_listings WHERE id=ANY($1::text[]) AND complex_id IS NOT NULL", [a, b]) == 0
    assert (await seed_listing_complexes(listing_ids=[a, b]))["examined"] == 0
    await pg.execute("UPDATE complex_aliases SET status='verified',reviewed_by='test' WHERE source_id=$1", a)
    await pg.execute("UPDATE apartment_listings SET address='Анет баба, 4/1' WHERE id=$1", a)
    again = await seed_listing_complexes(listing_ids=[a, b])
    assert again["created"] == 0 and again["observed"] == 1
    assert await pg.fetchval("SELECT status FROM complex_aliases WHERE source_id=$1 AND source=$2", a, SOURCE) == "verified"
    assert await pg.fetchval("SELECT count(*) FROM complex_addresses WHERE complex_id=$1 AND source=$2", cid, SOURCE) == 3


@pytest.mark.asyncio
async def test_verified_alias_and_explicit_binding_reuse_ids_without_new_complex(db):
    from bot.db import pg
    root = await _complex(db, "Canonical")
    alias_name = db["prefix"] + "Verified spelling"
    await pg.execute("INSERT INTO complex_aliases (complex_id,name,normalized_name,source,source_id,status) "
                     "VALUES ($1,$2,complex_name_key($2),'test',$2,'verified')", root, alias_name)
    a = await _listing(db, alias_name, "Улица, 1")
    b = await _listing(db, db["prefix"] + "Completely different observed name", "Улица, 2", cid=root)
    report = await seed_listing_complexes(listing_ids=[a, b])
    assert report["created"] == 0 and report["observed"] == 2
    rows = await pg.fetch("SELECT complex_id,source_id FROM complex_addresses WHERE source=$1 AND source_id=ANY($2::text[])", SOURCE, [a, b])
    assert {r["complex_id"] for r in rows} == {root}


@pytest.mark.asyncio
async def test_ambiguous_garbage_junk_and_unreviewed_alias_do_not_seed_duplicates(db):
    from bot.db import pg
    await _complex(db, "Ambiguous")
    await _complex(db, "Ambiguous ")
    await _complex(db, "Blocked", garbage=True)
    root = await _complex(db, "Real")
    observed_name = db["prefix"] + "Observed spelling"
    await pg.execute("INSERT INTO complex_aliases (complex_id,name,normalized_name,source,source_id) "
                     "VALUES ($1,$2,complex_name_key($2),'test',$2)", root, observed_name)
    lids = [await _listing(db, db["prefix"] + "Ambiguous", "A, 1"),
            await _listing(db, 'ЖК "' + db["prefix"] + 'Blocked"', "B, 2"),
            await _listing(db, db["prefix"] + " Продается 3-комнатная квартира", "C, 3"),
            await _listing(db, observed_name, "D, 4")]
    before = await pg.fetchval("SELECT count(*) FROM complexes")
    report = await seed_listing_complexes(listing_ids=lids)
    assert report["created"] == report["observed"] == 0
    assert await pg.fetchval("SELECT count(*) FROM complexes") == before


@pytest.mark.asyncio
async def test_observation_limit_advances_the_pending_queue(db):
    name = db["prefix"] + "Queue"
    lids = [await _listing(db, name, "Улица, 1"), await _listing(db, name, "Улица, 2")]
    first = await seed_listing_complexes(observation_limit=1, listing_ids=lids)
    second = await seed_listing_complexes(observation_limit=1, listing_ids=lids)
    last = await seed_listing_complexes(observation_limit=1, listing_ids=lids)
    assert first["examined"] == second["examined"] == 1
    assert first["created"] == 1 and second["created"] == 0
    assert first["observed"] == second["observed"] == 1
    assert last["examined"] == 0


@pytest.mark.asyncio
async def test_explicit_detachment_does_not_create_catalog_or_source_evidence(db):
    from bot.db import pg
    lid = await _listing(db, db["prefix"] + "Detached", "Улица, 4")
    await pg.execute("UPDATE apartment_listings SET complex_resolution='unbound' WHERE id=$1", lid)
    report = await seed_listing_complexes(listing_ids=[lid])
    assert report["examined"] == report["created"] == report["observed"] == 0
    assert await pg.fetchval("SELECT count(*) FROM complex_aliases WHERE source=$1 AND source_id=$2", SOURCE, lid) == 0


@pytest.mark.asyncio
async def test_listing_and_developer_import_share_normalized_name_lock(db):
    from bot.db import pg
    from newbuild_common import ComplexData, ensure_complex
    name = db["prefix"] + "Concurrent"
    lid = await _listing(db, ' ЖК «' + name + '» ', "Улица, 3")
    developer = await pg.fetchval("INSERT INTO developers (name) VALUES ($1) RETURNING id", db["prefix"] + "dev")
    db["developers"].append(developer)
    report, cid = await asyncio.gather(
        seed_listing_complexes(listing_ids=[lid]),
        ensure_complex(db["prefix"], developer,
                       ComplexData(source_id="1", name=name.upper(), address=None, housing_class=None, units=[])),
    )
    assert report["observed"] == 1
    rows = await pg.fetch("SELECT id FROM complexes WHERE complex_name_key(name)=complex_name_key($1)", name)
    assert [r["id"] for r in rows] == [cid]
    assert await pg.fetchval("SELECT complex_id FROM complex_aliases WHERE source=$1 AND source_id=$2", SOURCE, lid) == cid
