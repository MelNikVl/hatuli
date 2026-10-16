"""Execute the portable sitemap selection against an isolated in-memory fixture."""
import sqlite3

from bot.core.sitemap import COMPLEX_SITEMAP_SQL


def test_sitemap_excludes_quarantined_alias_and_duplicate_only_pages():
    with sqlite3.connect(':memory:') as db:
        db.executescript('''
            CREATE TABLE complexes (id INTEGER PRIMARY KEY, name TEXT, updated_at TEXT,
                is_street BOOLEAN, is_garbage BOOLEAN, canonical_id INTEGER, canonical_reason TEXT);
            CREATE TABLE apartment_listings (id INTEGER, complex_name TEXT,
                is_active BOOLEAN, is_duplicate BOOLEAN);
        ''')
        db.executemany('INSERT INTO complexes VALUES (?, ?, NULL, ?, ?, ?, ?)', [
            (1, 'Valid', False, False, None, None),
            (2, 'Alias', False, False, 1, None),
            (3, 'Garbage', False, True, None, None),
            (4, 'Street', True, False, None, None),
            (5, 'Quarantined', False, False, None, 'junk_unmatched'),
            (6, 'Duplicate only', False, False, None, None),
            (7, 'Inactive', False, False, None, None),
            (8, 'Also valid', None, None, None, None),
        ])
        db.executemany('INSERT INTO apartment_listings VALUES (?, ?, ?, ?)', [
            (1, 'Valid', True, False), (2, 'Valid', True, True),
            (3, 'Alias', True, False), (4, 'Garbage', True, False),
            (5, 'Street', True, False), (6, 'Quarantined', True, False),
            (7, 'Duplicate only', True, True), (8, 'Inactive', False, False),
            (9, 'Also valid', None, None),
        ])
        assert [(r[0], r[2]) for r in db.execute(COMPLEX_SITEMAP_SQL)] == [(1, 1), (8, 1)]
