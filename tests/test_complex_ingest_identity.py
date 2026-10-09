"""Import identity regressions; run against an explicit isolated DATABASE_URL."""
import asyncio
import os

import pytest
import pytest_asyncio

from bot.core.complex_ingest_identity import (
    AmbiguousComplexIdentity, InvalidComplexIdentity, normalize_complex_name,
    record_complex_observations, resolve_complex_name, resolve_ingest_complex,
)
from newbuild_common import ComplexData, UnitData, ensure_complex, save_complex
from bot.core.site_enrichment import save_enrichment

PFX = "__test_cxingest__"
SOURCE = PFX + "source"


@pytest_asyncio.fixture
async def db(monkeypatch):
    from bot.db.pg import init_pool, close_pool, execute
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        pytest.skip("Set DATABASE_URL to an isolated test database")
    await init_pool(dsn)
    await _cleanup(execute)

    async def no_geocode(*args):
        return None
    monkeypatch.setattr("newbuild_common.geocode_complex", no_geocode)
    yield
    await _cleanup(execute)
    await close_pool()


async def _cleanup(execute):
    # This fixture owns only the prefixed synthetic rows, never real entities.
    await execute("DELETE FROM newbuild_unit_price_history WHERE unit_id IN "
                  "(SELECT id FROM newbuild_units WHERE source LIKE $1)", PFX + "%")
    await execute("DELETE FROM newbuild_units WHERE source LIKE $1", PFX + "%")
    await execute("DELETE FROM source_changes WHERE source LIKE $1", PFX + "%")
    await execute("DELETE FROM complex_aliases WHERE complex_id IN "
                  "(SELECT id FROM complexes WHERE name LIKE $1)", PFX + "%")
    await execute("DELETE FROM complex_addresses WHERE complex_id IN "
                  "(SELECT id FROM complexes WHERE name LIKE $1)", PFX + "%")
    await execute("DELETE FROM complex_source_link_candidates WHERE source LIKE $1", PFX + "%")
    await execute("DELETE FROM complex_source_link_rejections WHERE source LIKE $1", PFX + "%")
    await execute("DELETE FROM complex_source_links WHERE source LIKE $1", PFX + "%")
    await execute("DELETE FROM complexes WHERE name LIKE $1", PFX + "%")
    await execute("DELETE FROM developers WHERE name LIKE $1", PFX + "%")


async def _complex(name, **fields):
    from bot.db.pg import fetchval
    return await fetchval("INSERT INTO complexes (name, canonical_id, canonical_reason) "
                          "VALUES ($1, $2, $3) RETURNING id",
                          PFX + name, fields.get("canonical_id"), fields.get("canonical_reason"))


async def _developer():
    from bot.db.pg import fetchval
    return await fetchval("INSERT INTO developers (name) VALUES ($1) RETURNING id", PFX + "dev")


def _data(source_id, name, address=None):
    return ComplexData(source_id=source_id, name=PFX + name, address=address,
                       housing_class=None, units=[])


@pytest.mark.parametrize("name,expected", [
    (' ЖК «Tumar   Club» ', 'tumar club'),
    ('\tЖК\n"Tumar Club"\t', 'tumar club'),
    ('КГ “Garden East 2 очередь”', 'garden east 2 очередь'),
    ('Нурсая-2', 'нурсая-2'),
    ('GreenLine. Garden корпус A', 'greenline. garden корпус a'),
    ('\u00a0ЖК\u00a0«Nexpo\u00a0Classic\u202f2»\u2009', 'nexpo classic 2'),
    ('Nexpo\u200b Classic', 'nexpo\u200b classic'),
    ('ЖК OİU', 'oi\u0307u'),
    (None, ''),
])
def test_normalization_keeps_phase_and_product(name, expected):
    assert normalize_complex_name(name) == expected


@pytest.mark.asyncio
async def test_sql_key_matches_python(db):
    from bot.db.pg import fetchval
    for name in (None, '', ' ЖК «Tumar   Club» ', '\tЖК\n"Tumar Club"\t',
                 'КГ “Garden East 2 очередь”', 'Нурсая-2', 'GreenLine. Garden корпус A',
                 "ЖК 'Тумар Клаб'", 'ЖК. Tumar Club', '  ЖК  Club West  ',
                 '\u00a0ЖК\u00a0«Nexpo\u00a0Classic\u202f2»\u2009', 'Nexpo\u200b Classic',
                 '\u200b\u00a0ЖК "Nexpo Classic"\u200b', 'ЖК OİU',
                 ''.join(chr(c) for c in [*range(9, 14), *range(28, 33), 133, 160, 5760,
                                         *range(8192, 8203), 8232, 8233, 8239, 8287, 12288])
                 + 'ЖК "Классик"'):
        assert await fetchval("SELECT complex_name_key($1)", name) == normalize_complex_name(name)


@pytest.mark.asyncio
async def test_renamed_source_reuses_canonical_and_preserves_spine(db):
    from bot.db.pg import execute, fetchval
    canonical = await _complex("canonical")
    alias = await _complex("legacy", canonical_id=canonical, canonical_reason="krisha_slug")
    await execute("INSERT INTO complex_source_links "
                  "(complex_id, source, source_id, match_method, confidence) "
                  "VALUES ($1, $2, 'stable-id', 'manual', 1)", alias, SOURCE)
    dev = await _developer()
    result = await ensure_complex(SOURCE, dev, _data("stable-id", "new-brand", "Анет баба, 4"))
    assert result == canonical
    assert await fetchval("SELECT complex_id FROM complex_source_links "
                          "WHERE source=$1 AND source_id='stable-id'", SOURCE) == alias
    assert await fetchval("SELECT count(*) FROM complexes WHERE name=$1", PFX + "new-brand") == 0
    assert await fetchval("SELECT status FROM complex_aliases WHERE complex_id=$1 AND name=$2",
                          canonical, PFX + "new-brand") == "observed"


@pytest.mark.asyncio
async def test_legacy_source_alias_preserves_cache_pair_and_seeds_original_spine(db):
    from bot.db.pg import execute, fetchrow, fetchval
    canonical = await _complex("canonical")
    alias = await _complex("legacy", canonical_id=canonical, canonical_reason="krisha_slug")
    await execute("UPDATE complexes SET newbuild_source=$2, newbuild_source_id='legacy-id' WHERE id=$1",
                  alias, SOURCE)
    dev = await _developer()
    assert await ensure_complex(SOURCE, dev, _data("legacy-id", "renamed")) == canonical
    assert await fetchval("SELECT complex_id FROM complex_source_links "
                          "WHERE source=$1 AND source_id='legacy-id'", SOURCE) == alias
    root = await fetchrow("SELECT newbuild_source, newbuild_source_id FROM complexes WHERE id=$1", canonical)
    assert root["newbuild_source"] is None and root["newbuild_source_id"] is None
    assert await fetchval("SELECT count(*) FROM complexes WHERE name LIKE $1", PFX + "%") == 2


@pytest.mark.asyncio
async def test_reimport_preserves_all_addresses_and_review_status(db):
    from bot.db.pg import execute, fetch, fetchval
    dev = await _developer()
    cid = await ensure_complex(SOURCE, dev, _data("one", "first-name", "Анет баба, 4"))
    await execute("UPDATE complex_aliases SET status='verified', reviewed_by='reviewer', reviewed_at=now() "
                  "WHERE complex_id=$1", cid)
    await execute("UPDATE complex_addresses SET status='rejected', reviewed_by='reviewer', reviewed_at=now() "
                  "WHERE complex_id=$1", cid)
    assert await ensure_complex(SOURCE, dev, _data("one", "first-name", "Анет баба, 4")) == cid
    assert await ensure_complex(SOURCE, dev, _data("one", "new-name", "Туркестан, 4Б")) == cid
    rows = await fetch("SELECT address, status FROM complex_addresses WHERE complex_id=$1 ORDER BY address", cid)
    assert [(r["address"], r["status"]) for r in rows] == [
        ("Анет баба, 4", "rejected"), ("Туркестан, 4Б", "observed")]
    assert await fetchval("SELECT address FROM complexes WHERE id=$1", cid) == "Анет баба, 4"
    assert await fetchval("SELECT status FROM complex_aliases WHERE complex_id=$1 AND name=$2",
                          cid, PFX + "first-name") == "verified"
    assert await fetchval("SELECT count(*) FROM complex_aliases WHERE complex_id=$1", cid) == 2


@pytest.mark.asyncio
async def test_unverified_alias_cannot_resolve_and_verified_alias_can(db):
    from bot.db.pg import execute, fetchval
    cid = await _complex("canonical")
    variant = PFX + "Тумар Клаб"
    await record_complex_observations(cid, SOURCE, "observed", variant, None)
    assert await resolve_ingest_complex(SOURCE, "different", variant) is None
    await execute("UPDATE complex_aliases SET status='verified' WHERE complex_id=$1", cid)
    assert await resolve_ingest_complex(SOURCE, "different", 'ЖК «' + variant + '»') == cid
    dev = await _developer()
    assert await ensure_complex(SOURCE, dev, ComplexData("different", 'ЖК «' + variant + '»', None, None, [])) == cid
    assert await fetchval("SELECT count(*) FROM complexes WHERE name LIKE $1", PFX + "%") == 1
    await execute("UPDATE complex_aliases SET status='rejected' WHERE complex_id=$1", cid)
    assert await resolve_ingest_complex(SOURCE, "other-unlinked", variant) is None


@pytest.mark.asyncio
async def test_ambiguous_exact_name_stops_import_without_creating_row(db):
    from bot.db.pg import fetchval
    first = await _complex("same")
    second = await _complex("same ")
    dev = await _developer()
    with pytest.raises(AmbiguousComplexIdentity) as exc:
        await ensure_complex(SOURCE, dev, _data("ambiguous", "SAME"))
    assert set(exc.value.candidate_ids) == {first, second}
    assert await resolve_complex_name(PFX + "same") is None
    assert await fetchval("SELECT count(*) FROM complex_source_links WHERE source=$1", SOURCE) == 0
    assert await fetchval("SELECT count(*) FROM complex_aliases WHERE source=$1", SOURCE) == 0
    assert await fetchval("SELECT count(*) FROM complexes WHERE name LIKE $1", PFX + "%") == 2


@pytest.mark.asyncio
async def test_canonical_aliases_do_not_create_ambiguity_but_shared_reviewed_keys_do(db):
    from bot.db.pg import execute
    first = await _complex("same")
    await _complex("same ", canonical_id=first, canonical_reason="krisha_slug")
    assert await resolve_ingest_complex(SOURCE, "fresh", PFX + "same") == first
    second = await _complex("separate-project")
    for cid in (first, second):
        await record_complex_observations(cid, SOURCE, str(cid), PFX + "shared", None)
    await execute("UPDATE complex_aliases SET status='verified' WHERE name=$1", PFX + "shared")
    with pytest.raises(AmbiguousComplexIdentity):
        await resolve_ingest_complex(SOURCE, "shared-import", PFX + "shared")


@pytest.mark.asyncio
async def test_phase_labels_remain_separate_and_same_address_does_not_merge(db):
    dev = await _developer()
    first = await ensure_complex(SOURCE, dev, _data("phase1", "Garden", "Одна улица, 4"))
    second = await ensure_complex(SOURCE, dev, _data("phase2", "Garden 2 очередь", "Одна улица, 4"))
    assert first != second
    assert await resolve_ingest_complex(SOURCE, "new-source", PFX + "Garden 2 очередь") == second


@pytest.mark.asyncio
async def test_concurrent_new_source_spellings_create_one_identity(db):
    from bot.db.pg import fetchval
    dev = await _developer()
    ids = await asyncio.gather(
        ensure_complex(SOURCE, dev, _data("concurrent", "original")),
        ensure_complex(SOURCE, dev, _data("concurrent", "renamed")),
    )
    assert ids[0] == ids[1]
    assert await fetchval("SELECT count(*) FROM complexes WHERE name LIKE $1", PFX + "%") == 1
    assert await fetchval("SELECT count(*) FROM complex_aliases WHERE complex_id=$1", ids[0]) == 2


@pytest.mark.asyncio
async def test_concurrent_different_sources_same_key_create_one_identity(db):
    from bot.db.pg import fetchval
    dev = await _developer()
    ids = await asyncio.gather(
        ensure_complex(SOURCE + "a", dev, _data("one", "same")),
        ensure_complex(SOURCE + "b", dev, _data("two", "SAME ")),
    )
    assert ids[0] == ids[1]
    assert await fetchval("SELECT count(*) FROM complexes WHERE name LIKE $1", PFX + "%") == 1
    assert await fetchval("SELECT count(*) FROM complex_source_links WHERE source LIKE $1", PFX + "%") == 2


@pytest.mark.asyncio
async def test_invalid_source_root_does_not_create_replacement(db):
    from bot.db.pg import execute, fetchval
    cid = await _complex("bad-root")
    await execute("UPDATE complexes SET is_garbage=TRUE WHERE id=$1", cid)
    await execute("INSERT INTO complex_source_links (complex_id, source, source_id, match_method) "
                  "VALUES ($1, $2, 'bad', 'manual')", cid, SOURCE)
    dev = await _developer()
    with pytest.raises(InvalidComplexIdentity):
        await ensure_complex(SOURCE, dev, _data("bad", "replacement"))
    assert await fetchval("SELECT count(*) FROM complexes WHERE name=$1", PFX + "replacement") == 0


@pytest.mark.asyncio
async def test_redirected_inventory_keeps_unit_ids_history_and_canonical_counters(db):
    from bot.db.pg import execute, fetchrow, fetchval
    dev = await _developer()
    original = _data("inventory", "original")
    original.units = [UnitData("unit1", price=100), UnitData("unit2", price=200)]
    await save_complex(SOURCE, dev, original, {})
    alias = await fetchval("SELECT id FROM complexes WHERE name=$1", original.name)
    canonical = await _complex("canonical-inventory")
    await execute("UPDATE complexes SET canonical_id=$2, canonical_reason='krisha_slug' WHERE id=$1",
                  alias, canonical)
    unit_id = await fetchval("SELECT id FROM newbuild_units WHERE source=$1 AND source_unit_id='unit1'", SOURCE)
    fresh = _data("inventory", "renamed")
    fresh.units = [UnitData("unit1", price=150)]
    stats = {}
    await save_complex(SOURCE, dev, fresh, stats)
    assert stats["units_seen"] == 1 and stats.get("units_new", 0) == 0
    assert stats["units_sold"] == 1 and stats["price_changes"] == 1
    unit = await fetchrow("SELECT id, complex_id, price FROM newbuild_units "
                         "WHERE source=$1 AND source_unit_id='unit1'", SOURCE)
    assert unit["id"] == unit_id and unit["complex_id"] == alias and unit["price"] == 150
    assert await fetchval("SELECT price FROM newbuild_unit_price_history WHERE unit_id=$1", unit_id) == 150
    counts = await fetchrow("SELECT newbuild_units_count, newbuild_sold_count FROM complexes WHERE id=$1", canonical)
    assert counts["newbuild_units_count"] == 1 and counts["newbuild_sold_count"] == 1


@pytest.mark.asyncio
async def test_catalog_enrichment_reuses_renamed_source_alias_and_preserves_all_observations(db):
    from bot.db.pg import execute, fetchval
    canonical = await _complex("canonical-enrichment")
    alias = await _complex("legacy-enrichment", canonical_id=canonical, canonical_reason="krisha_slug")
    url = "https://example.com/stable-catalog-card"
    await execute("INSERT INTO complex_source_links (complex_id,source,source_id,match_method) "
                  "VALUES ($1,$2,$3,'manual')", alias, SOURCE, url)
    first = {"name": PFX + "first-catalog-name", "url": url, "address": "Анет баба, 4"}
    assert (await save_enrichment({"irrelevant-parser-key": first}, SOURCE))["created"] == 0
    second = {"name": PFX + "renamed-catalog-name", "url": url, "address": "Туркестан, 4Б"}
    assert (await save_enrichment({"another-parser-key": second}, SOURCE))["changed"] == 1
    assert await fetchval("SELECT complex_id FROM complex_source_links WHERE source=$1 AND source_id=$2",
                          SOURCE, url) == alias
    assert await fetchval("SELECT count(*) FROM complexes WHERE name LIKE $1", PFX + "%") == 2
    assert await fetchval("SELECT count(*) FROM complex_aliases WHERE complex_id=$1", canonical) == 2
    assert await fetchval("SELECT count(*) FROM complex_addresses WHERE complex_id=$1", canonical) == 2


@pytest.mark.asyncio
async def test_legacy_source_info_url_prevents_renamed_catalog_duplicate(db):
    import json
    from bot.db.pg import execute, fetchval
    cid = await _complex("legacy-catalog")
    url = "https://example.com/catalog-card"
    await execute("UPDATE complexes SET source_info=$2::jsonb WHERE id=$1", cid,
                  json.dumps({SOURCE: {"url": url, "name": PFX + "legacy-catalog"}}))
    stats = await save_enrichment({"brand-key": {"name": PFX + "new-brand", "url": url}}, SOURCE)
    assert stats["matched"] == 1 and stats["created"] == 0
    assert await fetchval("SELECT complex_id FROM complex_source_links WHERE source=$1 AND source_id=$2",
                          SOURCE, url) == cid


@pytest.mark.asyncio
async def test_catalog_ambiguous_exact_name_is_skipped_without_mutation(db):
    from bot.db.pg import fetchval
    await _complex("same-catalog")
    await _complex("same-catalog ")
    stats = await save_enrichment({"parser-key": {"name": PFX + "same-catalog",
                                  "url": "https://example.com/ambiguous"}}, SOURCE)
    assert stats == {"matched": 0, "created": 0, "changed": 0}
    assert await fetchval("SELECT count(*) FROM complex_source_links WHERE source=$1", SOURCE) == 0
    assert await fetchval("SELECT count(*) FROM complex_aliases WHERE source=$1", SOURCE) == 0


@pytest.mark.asyncio
async def test_catalog_verified_alias_and_concurrent_source_sections(db):
    import json
    from bot.db.pg import execute, fetchval
    cid = await _complex("canonical-catalog")
    alias_name = PFX + "reviewed-catalog-spelling"
    await record_complex_observations(cid, SOURCE, "reviewed", alias_name, None)
    await execute("UPDATE complex_aliases SET status='verified' WHERE complex_id=$1", cid)
    stats = await asyncio.gather(
        save_enrichment({"one": {"name": alias_name, "url": "https://example.com/one"}}, SOURCE + "a"),
        save_enrichment({"two": {"name": PFX + "canonical-catalog", "url": "https://example.com/two"}}, SOURCE + "b"),
    )
    assert all(item["matched"] == 1 and item["created"] == 0 for item in stats)
    value = await fetchval("SELECT source_info FROM complexes WHERE id=$1", cid)
    stored = json.loads(value) if isinstance(value, str) else value
    assert set(stored) == {SOURCE + "a", SOURCE + "b"}
    assert await fetchval("SELECT count(*) FROM complexes WHERE name LIKE $1", PFX + "%") == 1
