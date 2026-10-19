"""Execute bulk identity and the existing single-page rule on isolated fixtures."""
import re
import sqlite3

import pytest

from bot.core.complex_membership import active_sale_membership_ctes_sql, listing_complex_match_sql
from bot.core.sitemap import COMPLEX_SITEMAP_SQL


def seed(db):
    db.executescript("""
        CREATE TABLE complexes (id INTEGER PRIMARY KEY, name TEXT, updated_at TEXT,
            is_street BOOLEAN, is_garbage BOOLEAN, canonical_id INTEGER,
            canonical_reason TEXT, parent_complex_id INTEGER);
        CREATE TABLE complex_aliases (complex_id INTEGER, normalized_name TEXT, status TEXT);
        CREATE TABLE apartment_listings (id TEXT PRIMARY KEY, complex_name TEXT,
            is_active BOOLEAN, is_duplicate BOOLEAN, complex_id INTEGER,
            complex_resolution TEXT, resolved_house_id INTEGER);
    """)
    db.executemany('INSERT INTO complexes VALUES (?, ?, NULL, ?, ?, ?, ?, ?)', [
        (1, 'Valid', False, False, None, None, None),
        (2, 'Old valid', False, False, 1, None, None),
        (3, 'Garbage', False, True, None, None, None),
        (4, 'Street', True, False, None, None, None),
        (5, 'Quarantined', False, False, None, 'junk_unmatched', None),
        (6, 'Duplicate only', False, False, None, None, None),
        (7, 'Inactive', False, False, None, None, None),
        (8, 'Also valid', None, None, None, None, None),
        (9, 'Umbrella', False, False, None, None, None),
        (10, 'House', False, False, None, None, 9),
        (11, 'Old house', False, False, 10, None, None),
        (12, 'Ambiguous', False, False, None, None, None),
        (13, 'Ambiguous', False, False, None, None, None),
        (14, 'Bad alias', False, True, 1, None, None),
        (15, 'Bad destination', False, False, 3, None, None),
        (16, 'Chained alias', False, False, 2, None, None),
        (17, 'Cycle', False, False, 17, None, None),
    ])
    db.executemany('INSERT INTO complex_aliases VALUES (?, ?, ?)', [
        (1, 'verified', 'verified'), (2, 'verified', 'verified'),
        (8, 'candidate', 'candidate'), (1, 'shared', 'verified'),
        (8, 'shared', 'verified'), (11, 'legacy house', 'verified'),
    ])
    rows = [
        ('fk_wins', 'Also valid', True, False, 1, None, 10),
        ('fk_alias', 'Wrong display name', True, False, 2, None, None),
        ('fk_unbound_flag', 'Wrong', True, False, 1, 'unbound', None),
        ('invalid_fk', 'Valid', True, False, 999, None, None),
        ('garbage_fk', 'Valid', True, False, 3, None, None),
        ('bad_alias', 'Valid', True, False, 14, None, None),
        ('bad_target', 'Valid', True, False, 15, None, None),
        ('chained', 'Valid', True, False, 16, None, None),
        ('cycle', 'Valid', True, False, 17, None, None),
        ('resolved_house', 'Umbrella', True, False, None, None, 10),
        ('resolved_alias', 'Wrong', True, False, None, None, 11),
        ('invalid_house', 'Valid', True, False, None, None, 999),
        ('unbound_house', 'Valid', True, False, None, 'unbound', 10),
        ('unbound_name', 'Valid', True, False, None, 'unbound', None),
        ('unique_name', '  ALSO VALID  ', None, None, None, None, None),
        ('legacy_name', 'Old valid', True, False, None, None, None),
        ('verified_name', 'Verified', True, False, None, None, None),
        ('verified_house', 'Legacy house', True, False, None, None, None),
        ('ambiguous_name', 'Ambiguous', True, False, None, None, None),
        ('ambiguous_alias', 'Shared', True, False, None, None, None),
        ('unverified_alias', 'Candidate', True, False, None, None, None),
        ('blank', ' ', True, False, None, None, None),
        ('inactive', 'Inactive', False, False, 7, None, None),
        ('duplicate', 'Duplicate only', True, True, 6, None, None),
        ('duplicate_fk', 'Valid', True, True, 1, None, None),
        ('quarantined', 'Quarantined', True, False, None, None, None),
    ]
    db.executemany('INSERT INTO apartment_listings VALUES (?, ?, ?, ?, ?, ?, ?)', rows)


@pytest.fixture
def database():
    with sqlite3.connect(':memory:') as db:
        db.create_function('btrim', 1, lambda s: s.strip() if s is not None else None)
        db.create_function('complex_name_key', 1,
                           lambda s: re.sub(r'\s+', ' ', s.strip().lower()) if s else '')
        seed(db)
        yield db


@pytest.mark.parametrize('include_children', [False, True])
def test_bulk_membership_matches_existing_page_rule(database, include_children):
    query = active_sale_membership_ctes_sql(include_children=include_children)
    bulk = set(database.execute(query + ' SELECT listing_id, complex_id FROM listing_membership'))
    expected = set()
    for (cid,) in database.execute('SELECT id FROM complexes WHERE canonical_id IS NULL'):
        # Only eligible public canonical pages are part of the bulk contract.
        if cid in (3, 4, 5):
            continue
        predicate = listing_complex_match_sql('$1', 'a', include_children=include_children)
        rows = database.execute('SELECT a.id FROM apartment_listings a WHERE '
                                'a.is_active IS NOT FALSE AND coalesce(a.is_duplicate,false)=false AND '
                                + predicate, {"1": cid})
        expected.update((row[0], cid) for row in rows)
    assert bulk == expected
    assert ('fk_wins', 8) not in bulk
    assert ('resolved_house', 10) in bulk
    assert (('resolved_house', 9) in bulk) == include_children
    assert not any(lid in {'invalid_fk', 'ambiguous_name', 'ambiguous_alias',
                          'unbound_name', 'unbound_house', 'invalid_house'} for lid, _ in bulk)


def test_sitemap_counts_canonical_and_child_membership_once(database):
    rows = list(database.execute(COMPLEX_SITEMAP_SQL))
    assert [(r[0], r[2]) for r in rows] == [(1, 5), (9, 3), (10, 3), (8, 1)]
    assert len({r[0] for r in rows}) == len(rows)


@pytest.mark.asyncio
@pytest.mark.parametrize('include_children', [False, True])
async def test_bulk_membership_in_isolated_postgres(include_children):
    import os
    from urllib.parse import urlsplit
    import asyncpg

    dsn = os.getenv('HATULI_SITEMAP_TEST_DSN') or os.getenv('DATABASE_URL', '')
    # Never run this writable fixture against a market database.
    if urlsplit(dsn).path not in {'/hatuli_test', '/hatuli_sitemap_test'}:
        pytest.skip('Requires an explicitly named isolated test database')

    class SeedOperations:
        def __init__(self):
            self.operations = []

        def executescript(self, sql):
            self.operations.append(('schema', sql))

        def executemany(self, sql, rows):
            self.operations.append(('rows', sql, rows))

    operations = SeedOperations()
    seed(operations)
    conn = await asyncpg.connect(dsn)
    try:
        # Connection-local temp tables shadow the migrated schema and disappear
        # on close. complex_name_key remains the real migrated PostgreSQL function.
        for kind, sql, *rows in operations.operations:
            if kind == 'schema':
                await conn.execute(sql.replace('CREATE TABLE', 'CREATE TEMP TABLE'))
            else:
                indexes = iter(range(1, sql.count('?') + 1))
                sql = re.sub(r'\?', lambda _: '$' + str(next(indexes)), sql)
                await conn.executemany(sql, rows[0])
        query = active_sale_membership_ctes_sql(include_children=include_children)
        bulk = {tuple(row) for row in await conn.fetch(
            query + ' SELECT listing_id, complex_id FROM listing_membership')}
        expected = set()
        for cid in (1, 6, 7, 8, 9, 10, 12, 13):
            predicate = listing_complex_match_sql('$1', 'a', include_children=include_children)
            rows = await conn.fetch('SELECT a.id FROM apartment_listings a WHERE '
                                    'a.is_active IS NOT FALSE AND coalesce(a.is_duplicate,false)=false AND '
                                    + predicate, cid)
            expected.update((row['id'], cid) for row in rows)
        assert bulk == expected
        sitemap = await conn.fetch(COMPLEX_SITEMAP_SQL)
        assert [(r['id'], r['active_cnt']) for r in sitemap] == [(1, 5), (9, 3), (10, 3), (8, 1)]
    finally:
        await conn.close()
