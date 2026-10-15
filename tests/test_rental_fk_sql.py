"""Rental SQL identity regressions; optional PostgreSQL uses temp tables only.

RENTAL_FK_TEST_DSN is opt-in, must be loopback, non-default-port and a database
named hatuli_test*. No app pool, migrations or permanent tables are touched.
"""
from datetime import datetime, timezone
import os
from urllib.parse import urlsplit

import pytest
import pytest_asyncio

from bot.core.complex_membership import rental_complex_match_sql, rental_identity_ctes_sql
from bot.core.comparison_rental_estimate import RENTAL_COMPARISON_SQL, SINGLE_OFFER_RENTAL_SQL

URL_A = "https://krisha.kz/complex/show/astana/project-a"
URL_B = "https://krisha.kz/complex/show/astana/project-b"
URL_FAMILY = "https://krisha.kz/complex/show/astana/family"


def test_sql_enforces_fk_priority_source_uniqueness_and_no_nearest_guess():
    sql = rental_complex_match_sql(listing_alias="r", include_children=True)
    assert "r.complex_id IS NOT NULL" in sql and "r.complex_id IS NULL" in sql
    assert "source.cid=canonical.parent_id" in sql
    assert "count(DISTINCT cid)=1" in sql
    assert "a.status='verified'" in sql
    assert "rental_canonical_map WHERE id=r.complex_id" in sql
    assert "r.complex_id IS NULL AND nk.n=" in RENTAL_COMPARISON_SQL
    assert "r.rent_period='monthly'" in RENTAL_COMPARISON_SQL
    assert "NOT coalesce(r.rent_period_conflict,false)" in RENTAL_COMPARISON_SQL
    assert "NOT coalesce(canonical.is_umbrella,false)" in RENTAL_COMPARISON_SQL
    assert "bound.id=$2" in SINGLE_OFFER_RENTAL_SQL and "r.rooms=$3" in SINGLE_OFFER_RENTAL_SQL
    assert "cardinality(walk.visited)<32" in rental_identity_ctes_sql()
    assert all(token not in sql.lower() for token in ("r.lat", "r.lon", "st_distance", "nearest"))


@pytest.mark.parametrize("kwargs", [{"complex_param":"$1;DROP TABLE x"}, {"listing_alias":"r;DROP TABLE x"}])
def test_membership_sql_rejects_untrusted_identifiers(kwargs):
    with pytest.raises(ValueError):
        rental_complex_match_sql(**kwargs)


@pytest_asyncio.fixture
async def sql_connection():
    dsn = os.environ.get("RENTAL_FK_TEST_DSN")
    if not dsn:
        pytest.skip("Set RENTAL_FK_TEST_DSN for opt-in disposable PostgreSQL temp-table tests")
    parsed = urlsplit(dsn)
    if (parsed.scheme not in ("postgres", "postgresql") or parsed.hostname != "127.0.0.1"
            or parsed.port in (None, 5432) or not parsed.path.startswith("/hatuli_test")):
        pytest.fail("RENTAL_FK_TEST_DSN must identify an isolated loopback test database")
    import asyncpg
    connection = await asyncpg.connect(dsn, command_timeout=15)
    try:
        # A dedicated connection shadows every source table in its temp schema.
        await connection.execute("""
            CREATE TEMP TABLE complexes(id INT PRIMARY KEY,name TEXT,canonical_id INT,
              canonical_reason TEXT,is_garbage BOOLEAN DEFAULT FALSE,is_street BOOLEAN DEFAULT FALSE,
              is_umbrella BOOLEAN DEFAULT FALSE,parent_complex_id INT,krisha_url TEXT);
            CREATE TEMP TABLE complex_aliases(complex_id INT,normalized_name TEXT,status TEXT);
            CREATE TEMP TABLE complex_source_links(complex_id INT,source TEXT,url TEXT,match_method TEXT,confidence REAL);
            CREATE TEMP TABLE rental_listings(id TEXT PRIMARY KEY,complex_id INT,complex_name TEXT,complex_url TEXT,
              price INT DEFAULT 250000,area REAL DEFAULT 60,rooms INT DEFAULT 2,floor INT DEFAULT 4,floors_total INT DEFAULT 12,
              city TEXT DEFAULT 'astana',prop_type TEXT DEFAULT 'apartment',is_active BOOLEAN DEFAULT TRUE,
              is_duplicate BOOLEAN DEFAULT FALSE,dup_needs_review BOOLEAN DEFAULT FALSE,duplicate_of TEXT,
              archived_at TIMESTAMPTZ,title TEXT DEFAULT 'Квартира',last_seen TIMESTAMPTZ DEFAULT now(),
              rent_period TEXT DEFAULT 'monthly',rent_period_conflict BOOLEAN DEFAULT FALSE);
            CREATE FUNCTION pg_temp.complex_name_key(TEXT) RETURNS TEXT LANGUAGE SQL IMMUTABLE AS
              $$ SELECT lower(btrim($1)) $$;
            SET search_path=pg_temp,public;
        """)
        await connection.executemany("INSERT INTO complexes(id,name,canonical_id,parent_complex_id,krisha_url,is_umbrella,is_garbage) VALUES($1,$2,$3,$4,$5,$6,$7)", [
            (1,"Project A",None,None,URL_A,False,False), (2,"Project B",None,None,URL_B,False,False),
            (3,"Family",None,None,URL_FAMILY,True,False), (4,"Family Block",None,3,None,False,False),
            (5,"Child alias",4,None,None,False,False), (6,"Child alias chain",5,None,None,False,False),
            (7,"Garbage",None,None,None,False,True), (8,"Cycle 8",9,None,None,False,False),
            (9,"Cycle 9",8,None,None,False,False), (10,"Family other child",None,3,None,False,False),
        ])
        yield connection
    finally:
        await connection.close()


async def insert(connection, fk, name, url, **changes):
    await connection.execute("DELETE FROM rental_listings")
    await connection.execute("INSERT INTO rental_listings(id,complex_id,complex_name,complex_url,rent_period,rent_period_conflict) VALUES('123456',$1,$2,$3,$4,$5)",
                             fk, name, url, changes.get("rent_period", "monthly"), changes.get("rent_period_conflict", False))


async def member(connection, target, *, include_children=False):
    return await connection.fetchval("SELECT count(*) FROM rental_listings r WHERE "+rental_complex_match_sql(listing_alias="r", include_children=include_children), target)


async def comparisons(connection):
    return await connection.fetch(RENTAL_COMPARISON_SQL, datetime.now(timezone.utc))


@pytest.mark.asyncio
@pytest.mark.parametrize("fk", [4,5,6])
async def test_existing_child_fk_and_redirect_chain_win_over_stale_name(sql_connection, fk):
    connection = sql_connection
    await insert(connection, fk, "Project B", URL_FAMILY)
    assert await member(connection, 4) == 1
    assert await member(connection, 2) == 0
    assert await member(connection, 3) == 0
    assert await member(connection, 3, include_children=True) == 1
    rows = await comparisons(connection)
    assert len(rows) == 1 and rows[0]["complex_id"] == 4
    narrowed = await connection.fetch(SINGLE_OFFER_RENTAL_SQL, datetime.now(timezone.utc), 6, 2, 60)
    assert len(narrowed) == 1 and narrowed[0]["complex_id"] == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("fk", [2,7,8,999])
async def test_wrong_ineligible_cyclic_or_missing_fk_never_falls_back_to_name(sql_connection, fk):
    await insert(sql_connection, fk, "Project A", URL_A)
    assert await member(sql_connection, 1) == 0
    assert await comparisons(sql_connection) == []


@pytest.mark.asyncio
async def test_null_fk_needs_unique_verified_name_and_its_own_source(sql_connection):
    connection = sql_connection
    await connection.execute("INSERT INTO complex_aliases VALUES(1,'legacy reviewed','verified'),(1,'unreviewed','observed')")
    await insert(connection, None, "legacy reviewed", URL_A)
    assert await member(connection, 1) == 1
    assert (await comparisons(connection))[0]["complex_id"] == 1
    await connection.execute("INSERT INTO complex_aliases VALUES(2,'legacy reviewed','verified')")
    assert await member(connection, 1) == 0 and await comparisons(connection) == []
    await insert(connection, None, "unreviewed", URL_A)
    assert await member(connection, 1) == 0 and await comparisons(connection) == []
    await insert(connection, None, "Family Block", URL_FAMILY)
    assert await member(connection, 4) == 0 and await comparisons(connection) == []


@pytest.mark.asyncio
async def test_source_url_ambiguity_is_rejected_even_with_correct_fk(sql_connection):
    connection = sql_connection
    await insert(connection, 1, "Project A", URL_A)
    assert len(await comparisons(connection)) == 1
    await connection.execute("INSERT INTO complex_source_links VALUES(2,'krisha',$1,'manual',1)", URL_A)
    assert await member(connection, 1) == 0 and await comparisons(connection) == []


@pytest.mark.asyncio
async def test_only_reviewed_or_high_confidence_source_links_qualify(sql_connection):
    connection = sql_connection
    custom = "https://krisha.kz/complex/show/astana/alternate"
    await insert(connection, 1, "Project B", custom)
    await connection.execute("INSERT INTO complex_source_links VALUES(1,'krisha',$1,'automatic',0.5)", custom)
    assert await member(connection, 1) == 0 and await comparisons(connection) == []
    await connection.execute("UPDATE complex_source_links SET confidence=0.9")
    assert await member(connection, 1) == 1 and len(await comparisons(connection)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("period,conflict", [("daily",False),("unknown",False),("monthly",True)])
async def test_period_and_conflict_gates_remain_in_comparison(sql_connection, period, conflict):
    await insert(sql_connection, 1, "Project A", URL_A, rent_period=period, rent_period_conflict=conflict)
    assert await comparisons(sql_connection) == []
