"""Execute developer aggregates against independent legacy membership rules."""
from collections import Counter
from datetime import datetime, timedelta, timezone
import os
import re
import sqlite3
from urllib.parse import urlsplit

import pytest

from bot.core.complex_membership import listing_complex_match_sql
from bot.core.developer_detail import DEVELOPER_COMPLEXES_SQL
from tests.test_sitemap_complexes import seed as membership_seed


START = datetime(2026, 1, 1, tzinfo=timezone.utc)
DURATION_SQL = "EXTRACT(EPOCH FROM (archived_at - first_seen))/86400"


def seed(database):
    membership_seed(database)
    database.executescript("""
        ALTER TABLE complexes ADD COLUMN developer_id INTEGER;
        ALTER TABLE complexes ADD COLUMN district TEXT;
        ALTER TABLE complexes ADD COLUMN year_built INTEGER;
        ALTER TABLE complexes ADD COLUMN housing_class TEXT;
        ALTER TABLE complexes ADD COLUMN photo_url TEXT;
        ALTER TABLE complexes ADD COLUMN lat REAL;
        ALTER TABLE complexes ADD COLUMN lon REAL;
        ALTER TABLE complexes ADD COLUMN listings_count INTEGER;
        ALTER TABLE complexes ADD COLUMN sold_count INTEGER;
        ALTER TABLE complexes ADD COLUMN avg_price_m2 REAL;
        ALTER TABLE apartment_listings ADD COLUMN address TEXT;
        ALTER TABLE apartment_listings ADD COLUMN first_seen TIMESTAMPTZ;
        ALTER TABLE apartment_listings ADD COLUMN archived_at TIMESTAMPTZ;
        ALTER TABLE apartment_listings ADD COLUMN complex_url TEXT;
        UPDATE complexes SET developer_id=7,listings_count=id*7,sold_count=id*3,
               district='Fixture district',year_built=2020,avg_price_m2=123456;
        -- Child 10 and ambiguous-name rival 13 belong to another developer.
        UPDATE complexes SET developer_id=8 WHERE id IN (8,10,13);
        INSERT INTO complexes (id,name,developer_id) VALUES (18,'Empty project',7);
    """)
    # Active archived rows are intentionally retained: the previous average
    # does not apply an is_active restriction. Duplicate rows affect address
    # frequencies but never the archive-duration average.
    observations = [
        ("Address B", START, START + timedelta(days=10), "https://krisha.kz/complex/show/conflict", "fk_wins"),
        ("Address B", START, START + timedelta(days=20), None, "fk_alias"),
        ("Address A", START, START + timedelta(days=200), None, "duplicate_fk"),
        ("House 20/1", START, START + timedelta(days=8), None, "resolved_house"),
        ("House 20/1", START, START + timedelta(days=12), None, "resolved_alias"),
        ("House 20/1", START, None, None, "verified_house"),
        ("   ", START, START + timedelta(days=50), None, "inactive"),
        ("Alpha", START, None, None, "unique_name"),
        ("Duplicate-only address", START, START + timedelta(days=100), None, "duplicate"),
    ]
    database.executemany("UPDATE apartment_listings SET address=?,first_seen=?,archived_at=?,complex_url=? WHERE id=?", observations)
    database.executemany("""INSERT INTO apartment_listings
        (id,complex_id,complex_name,is_active,is_duplicate,address,first_seen,archived_at)
        VALUES (?,?,?,?,?,?,?,?)""", [
        ("dup-address-2", 1, "Wrong", True, True, "Address A", START, START + timedelta(days=500)),
        ("dup-address-3", 1, "Wrong", False, True, "Address A", START, START + timedelta(days=800)),
        ("null-dup", 1, "Wrong", None, None, "", START, START + timedelta(days=30)),
        ("missing-start", 1, "Wrong", False, False, None, None, START + timedelta(days=90)),
        ("foreign-page", 8, "Wrong", True, False, "Beta", START, START + timedelta(days=14)),
        ("space-address", 7, "Wrong", True, False, "   ", None, None),
        ("external-name-rival", None, "Ambiguous", True, False, "Must not bind", START, START + timedelta(days=999)),
    ])


def duration(end, start):
    if end is None or start is None:
        return None
    def timestamp(value):
        return datetime.fromisoformat(value) if isinstance(value, str) else value
    return (timestamp(end) - timestamp(start)).total_seconds() / 86400


@pytest.fixture
def database():
    with sqlite3.connect(":memory:") as database:
        database.row_factory = sqlite3.Row
        database.create_function("btrim", 1, lambda value: value.strip() if value is not None else None)
        database.create_function("complex_name_key", 1, lambda value: re.sub(r"\s+", " ", value.strip().lower()) if value else "")
        database.create_function("archive_duration_days", 2, duration)
        seed(database)
        yield database


def expected(database, developer):
    """Independent per-page rule, then ordinary Python aggregate arithmetic."""
    projects = database.execute("""SELECT * FROM complexes WHERE developer_id=?
        AND coalesce(is_street,false)=false AND coalesce(is_garbage,false)=false
        AND canonical_id IS NULL AND coalesce(canonical_reason,'')<>'junk_unmatched'""", (developer,))
    result = {}
    for project in projects:
        predicate = listing_complex_match_sql("$1", "al", include_children=True)
        listings = list(database.execute("SELECT * FROM apartment_listings al WHERE " + predicate, {"1": project["id"]}))
        spans = [duration(row["archived_at"], row["first_seen"]) for row in listings
                 if not row["is_duplicate"] and row["archived_at"] is not None]
        spans = [value for value in spans if value is not None]
        frequencies = Counter(row["address"] for row in listings if row["address"] is not None and row["address"] != "")
        modes = {value for value, count in frequencies.items() if count == max(frequencies.values())} if frequencies else {None}
        result[project["id"]] = {"average": sum(spans) / len(spans) if spans else None,
                                  "addresses": modes, "active": project["listings_count"] or 0,
                                  "archived": project["sold_count"] or 0}
    return result


def assert_expected(rows, wanted):
    assert {row["id"] for row in rows} == set(wanted)
    for row in rows:
        target = wanted[row["id"]]
        assert row["address"] in target["addresses"]
        assert row["active_cnt"] == target["active"]
        assert row["sold_cnt"] == target["archived"]
        if target["average"] is None:
            assert row["avg_days_to_sell"] is None
        else:
            assert float(row["avg_days_to_sell"]) == pytest.approx(target["average"])


@pytest.mark.parametrize("developer", [7, 8, 999])
def test_bulk_aggregates_match_legacy_membership_with_separate_duplicate_scopes(database, developer):
    # SQLite executes all production CTEs, joins, windows and aggregates. Only
    # PostgreSQL's existing timestamp subtraction syntax needs an adapter.
    sql = DEVELOPER_COMPLEXES_SQL.replace(DURATION_SQL, "archive_duration_days(archived_at,first_seen)")
    rows = list(database.execute(sql, {"1": developer}))
    assert_expected(rows, expected(database, developer))
    by_id = {row["id"]: row for row in rows}
    if developer == 7:
        assert by_id[1]["avg_days_to_sell"] == 20
        assert by_id[1]["address"] == "Address A"  # Duplicate rows win address, not days.
        assert by_id[9]["avg_days_to_sell"] == 10  # Child belongs to a different developer.
        assert by_id[9]["address"] == "House 20/1"
        assert by_id[7]["address"] == "   "  # Do not silently broaden old empty-address filter.
        assert by_id[6]["avg_days_to_sell"] is None
        assert by_id[18]["address"] is by_id[18]["avg_days_to_sell"] is None
        assert by_id[18]["active_cnt"] == by_id[18]["sold_cnt"] == 0
        assert by_id[12]["avg_days_to_sell"] is None  # Rival outside developer still matters.
    if developer == 8:
        assert by_id[8]["address"] == "Alpha"  # Tied modes now have a deterministic tie break.
        assert by_id[10]["avg_days_to_sell"] == 10


@pytest.mark.asyncio
async def test_real_postgres_query_parity_in_explicit_ephemeral_database(database):
    import asyncpg
    dsn = os.getenv("HATULI_DEVELOPER_TEST_DSN") or os.getenv("DATABASE_URL", "")
    parsed = urlsplit(dsn)
    if (parsed.hostname not in {"localhost", "127.0.0.1", "::1"} or parsed.port == 1
            or parsed.path not in {"/hatuli_test", "/hatuli_developer_test", "/hatuli_sitemap_test"}):
        pytest.skip("Requires an explicitly named loopback-only isolated test database")

    class Operations:
        def __init__(self):
            self.items = []
        def executescript(self, sql):
            self.items.append((sql, None))
        def executemany(self, sql, rows):
            self.items.append((sql, rows))

    operations = Operations()
    seed(operations)
    connection = await asyncpg.connect(dsn, command_timeout=5)
    try:
        # Temp tables shadow the migrated test schema only in this connection.
        # No init_pool/migrations or permanent table writes are performed here.
        for sql, rows in operations.items:
            if rows is None:
                await connection.execute(sql.replace("CREATE TABLE", "CREATE TEMP TABLE"))
            else:
                indexes = iter(range(1, sql.count("?") + 1))
                sql = re.sub(r"\?", lambda match: "$" + str(next(indexes)), sql)
                await connection.executemany(sql, rows)
        for developer in (7, 8, 999):
            rows = await connection.fetch(DEVELOPER_COMPLEXES_SQL, developer)
            assert_expected(rows, expected(database, developer))
    finally:
        await connection.close()
