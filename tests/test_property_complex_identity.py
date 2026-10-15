"""Property Identity consumes canonical binding IDs and reviewed aliases safely."""
import os
import uuid

import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def db():
    from bot.db.pg import init_pool, close_pool, execute, fetch
    await init_pool(os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot"))
    state = {"prefix": "__test_pcx_" + uuid.uuid4().hex, "complexes": [], "listings": []}
    try:
        yield state
    finally:
        ids = state["listings"]
        properties = [r["property_id"] for r in await fetch(
            "SELECT property_id FROM property_listings WHERE listing_id = ANY($1::text[])", ids)]
        await execute("DELETE FROM property_match_candidates WHERE listing_id = ANY($1::text[]) "
                      "OR candidate_property_id = ANY($2::int[])", ids, properties)
        await execute("DELETE FROM property_listings WHERE listing_id = ANY($1::text[])", ids)
        await execute("DELETE FROM properties WHERE property_id = ANY($1::int[])", properties)
        await execute("DELETE FROM apartment_listings WHERE id = ANY($1::text[])", ids)
        await execute("DELETE FROM complex_aliases WHERE complex_id = ANY($1::int[])", state["complexes"])
        await execute("DELETE FROM complexes WHERE id = ANY($1::int[])", state["complexes"])
        await close_pool()


async def _complex(state, name, *, canonical_id=None, parent_id=None, garbage=False):
    from bot.db.pg import fetchval
    cid = await fetchval(
        "INSERT INTO complexes (name, canonical_id, parent_complex_id, is_garbage) "
        "VALUES ($1, $2, $3, $4) RETURNING id",
        state["prefix"] + name, canonical_id, parent_id, garbage)
    state["complexes"].append(cid)
    return cid


async def _listing(state, suffix, *, name=None, complex_id=None, house_id=None):
    from bot.db.pg import execute, fetchrow
    lid = state["prefix"] + suffix
    state["listings"].append(lid)
    await execute(
        "INSERT INTO apartment_listings (id, url, address, floor, area, rooms, complex_name, complex_id, resolved_house_id) "
        "VALUES ($1, $1, $2, 5, 45.0, 2, $3, $4, $5)",
        lid, lid + " улица, 10", state["prefix"] + name if name else None, complex_id, house_id)
    return dict(await fetchrow("SELECT * FROM apartment_listings WHERE id=$1", lid))


@pytest.mark.asyncio
async def test_explicit_binding_wins_over_stale_name_and_house_in_bootstrap(db):
    from bot.identity.property_linker import bootstrap_all_provisional
    root = await _complex(db, "Канон")
    alias = await _complex(db, "Старое имя", canonical_id=root)
    rival = await _complex(db, "Другой ЖК")
    row = await _listing(db, "explicit", name="Другой ЖК", complex_id=alias, house_id=rival)
    mapping = await bootstrap_all_provisional([row], dry_run=True)
    assert mapping[row["id"]]["row"]["_complex_id"] == root


@pytest.mark.asyncio
async def test_child_binding_does_not_collapse_into_umbrella_name(db):
    from bot.identity.property_linker import _resolve_complex_id
    parent = await _complex(db, "Зонтик")
    child = await _complex(db, "Зонтик корпус 1", parent_id=parent)
    row = await _listing(db, "house", name="Зонтик", house_id=child)
    assert await _resolve_complex_id(row) == child


@pytest.mark.asyncio
async def test_legacy_caller_reads_stored_explicit_binding_before_name(db):
    from bot.identity.property_linker import link_listing_to_property
    from bot.db.pg import fetchval
    root = await _complex(db, "Реальный ЖК")
    rival = await _complex(db, "Устаревший ЖК")
    row = await _listing(db, "legacy", name="Устаревший ЖК", complex_id=root)
    row.pop("complex_id")
    row.pop("resolved_house_id")
    result = await link_listing_to_property(row)
    assert await fetchval("SELECT complex_id FROM properties WHERE property_id=$1", result["property_id"]) == root
    assert root != rival


@pytest.mark.asyncio
async def test_ambiguous_names_and_unreviewed_aliases_remain_unknown(db):
    from bot.identity.property_linker import _resolve_complex_id
    from bot.db.pg import execute
    root = await _complex(db, "Одно имя")
    await _complex(db, "Одно имя ")
    row = await _listing(db, "ambiguous", name="Одно имя")
    assert await _resolve_complex_id(row) is None
    alias = db["prefix"] + "Другое написание"
    await execute(
        "INSERT INTO complex_aliases (complex_id, name, normalized_name, source, source_id) "
        "VALUES ($1, $2, complex_name_key($2), 'test', $2)", root, alias)
    row["complex_name"] = alias
    assert await _resolve_complex_id(row) is None
    await execute("UPDATE complex_aliases SET status='verified' WHERE complex_id=$1", root)
    assert await _resolve_complex_id(row) == root


@pytest.mark.asyncio
async def test_invalid_explicit_id_does_not_fall_back_to_a_different_name(db):
    from bot.identity.property_linker import _resolve_complex_id
    await _complex(db, "Чужой ЖК")
    junk = await _complex(db, "Мусор", garbage=True)
    row = await _listing(db, "junk", name="Чужой ЖК", complex_id=junk)
    assert await _resolve_complex_id(row) is None


@pytest.mark.asyncio
async def test_explicit_detachment_blocks_house_name_and_legacy_missing_resolution(db):
    from bot.identity.property_linker import _resolve_complex_id
    from bot.db.pg import execute
    root = await _complex(db, "Detached")
    row = await _listing(db, "unbound", name="Detached", house_id=root)
    await execute("UPDATE apartment_listings SET complex_resolution='unbound' WHERE id=$1", row["id"])
    row["complex_resolution"] = "unbound"
    assert await _resolve_complex_id(row) is None
    row.pop("complex_resolution")
    assert await _resolve_complex_id(row) is None
    # The explicit ID remains authoritative in a contradictory caller snapshot.
    row["complex_id"] = root
    row["complex_resolution"] = "unbound"
    assert await _resolve_complex_id(row) == root


@pytest.mark.asyncio
async def test_old_alias_property_yields_candidate_without_hard_link_or_rewrite(db):
    from bot.identity.property_linker import link_listing_to_property, _corroborating_base_methods
    from bot.db.pg import execute, fetchrow, fetchval
    root = await _complex(db, "Канон")
    alias = await _complex(db, "Дубль", canonical_id=root)
    old = await _listing(db, "old", complex_id=root)
    original = await link_listing_to_property(old)
    await execute("UPDATE properties SET complex_id=$2 WHERE property_id=$1", original["property_id"], alias)
    new = await _listing(db, "new", name="Неизвестное имя", complex_id=root)
    result = await link_listing_to_property(new)
    fuzzy = [c for c in result["candidates"] if c["match_method"] == "fuzzy"]
    assert len(fuzzy) == 1 and fuzzy[0]["candidate_property_id"] == original["property_id"]
    assert result["property_id"] != original["property_id"]
    assert await fetchval("SELECT complex_id FROM properties WHERE property_id=$1", original["property_id"]) == alias
    prop = dict(await fetchrow("SELECT * FROM properties WHERE property_id=$1", original["property_id"]))
    assert "fuzzy" in await _corroborating_base_methods(new, prop)


@pytest.mark.asyncio
async def test_incremental_snapshot_and_created_property_preserve_binding_id(db):
    from bot.jobs.property_identity_incremental import _fetch_unlinked, run_incremental
    from bot.db.pg import fetchval
    root = await _complex(db, "Правильный ЖК")
    await _complex(db, "Чужое имя")
    row = await _listing(db, "incremental", name="Чужое имя", complex_id=root)
    snapshot = await _fetch_unlinked(None, None, [row["id"]])
    assert snapshot[0]["complex_id"] == root
    assert "resolved_house_id" in snapshot[0]
    report = await run_incremental(listing_ids=[row["id"]])
    assert report["provisional_created"] == 1
    assert await fetchval("SELECT p.complex_id FROM properties p JOIN property_listings pl USING(property_id) "
                          "WHERE pl.listing_id=$1", row["id"]) == root
