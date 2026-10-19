"""Native SQL checks only on an explicitly isolated localhost test database.

Uses temporary tables and raw asyncpg; never initializes/migrates any DB.
The CI workflow applies migrations to its ephemeral PostgreSQL first.
"""
from datetime import datetime, timedelta, timezone
import os
from urllib.parse import urlsplit

import pytest
import pytest_asyncio

from bot.core.sale_detail_persistence import save_source_details


def _test_dsn():
    dsn = os.environ.get('HATULI_TEST_DATABASE_URL') or os.environ.get('DATABASE_URL', '')
    try:
        parsed = urlsplit(dsn)
        allowed = (parsed.hostname in ('localhost', '127.0.0.1') and parsed.port not in (None, 1)
                   and parsed.path == '/hatuli_test')
    except ValueError:
        allowed = False
    if not allowed:
        pytest.skip('Requires the isolated localhost hatuli_test PostgreSQL used by CI')
    return dsn


@pytest_asyncio.fixture
async def detail_db():
    import asyncpg
    connection = await asyncpg.connect(_test_dsn(), command_timeout=10)
    transaction = connection.transaction()
    await transaction.start()
    try:
        columns = await connection.fetch("SELECT column_name, is_nullable, column_default FROM information_schema.columns "
                                         "WHERE table_schema='public' AND table_name='apartment_listings' "
                                         "AND column_name IN ('source_details_checked_at','source_details_attempted_at')")
        assert len(columns) == 2, 'CI must apply migration 113 before testing'
        assert all(r['is_nullable'] == 'YES' and r['column_default'] is None for r in columns)
        await connection.execute('CREATE TEMP TABLE apartment_listings (LIKE public.apartment_listings INCLUDING DEFAULTS) ON COMMIT DROP')
        await connection.execute('SET LOCAL search_path=pg_temp,public')
        await connection.execute("INSERT INTO apartment_listings(id,price,floor,floors_total,year_built,description,renovation,furniture,details_fetched) "
                                 "VALUES ('123456789',30000000,5,10,2000,'old','old','true',TRUE)")
        yield connection
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.mark.asyncio
async def test_legacy_boolean_has_no_observation_timestamp(detail_db):
    row = await detail_db.fetchrow("SELECT * FROM apartment_listings WHERE id='123456789'")
    assert row['details_fetched'] is True
    assert row['source_details_checked_at'] is None and row['source_details_attempted_at'] is None


@pytest.mark.asyncio
async def test_real_pg_observed_null_empty_false_and_absent_values(detail_db):
    now = datetime.now(timezone.utc)
    await save_source_details({'id': '123456789', '_source_details_checked_at': now.isoformat(),
                               '_source_details_attempted_at': now.isoformat(),
                               '_source_detail_values': {'floor': None, 'description': '', 'furniture': False,
                                                         'photos': ['https://example.invalid/observed.jpg'], 'views_count': 0}},
                              execute=detail_db.execute)
    row = await detail_db.fetchrow("SELECT * FROM apartment_listings WHERE id='123456789'")
    assert row['floor'] is None and row['description'] == '' and row['furniture'] == 'false'
    assert row['floors_total'] == 10 and row['year_built'] == 2000 and row['renovation'] == 'old'
    assert row['views_count'] == 0 and row['source_details_checked_at'] == now


@pytest.mark.asyncio
async def test_real_pg_older_response_cannot_overwrite_newer_fields_but_attempt_advances(detail_db):
    now = datetime.now(timezone.utc)
    await save_source_details({'id': '123456789', '_source_details_checked_at': now.isoformat(),
                               '_source_detail_values': {'floor': 8, 'renovation': 'fresh'}}, execute=detail_db.execute)
    attempted = now + timedelta(seconds=1)
    await save_source_details({'id': '123456789', '_source_details_checked_at': (now-timedelta(hours=1)).isoformat(),
                               '_source_details_attempted_at': attempted.isoformat(),
                               '_source_detail_values': {'floor': 2, 'renovation': 'stale'}}, execute=detail_db.execute)
    row = await detail_db.fetchrow("SELECT * FROM apartment_listings WHERE id='123456789'")
    assert row['floor'] == 8 and row['renovation'] == 'fresh' and row['source_details_checked_at'] == now
    assert row['source_details_attempted_at'] == attempted


@pytest.mark.asyncio
async def test_real_pg_cast_error_cannot_commit_success_timestamp(detail_db):
    now = datetime.now(timezone.utc)
    async with detail_db.transaction():
        # Savepoint contains the failed statement; the prior row survives.
        with pytest.raises(Exception):
            async with detail_db.transaction():
                await save_source_details({'id': '123456789', '_source_details_checked_at': now.isoformat(),
                                           '_source_detail_values': {'floor': 'not-an-integer'}}, execute=detail_db.execute)
        row = await detail_db.fetchrow("SELECT * FROM apartment_listings WHERE id='123456789'")
        assert row['floor'] == 5 and row['source_details_checked_at'] is None


@pytest.mark.asyncio
async def test_real_new_catalog_insert_then_observed_identity_and_details_patch(detail_db, monkeypatch):
    from bot.db import pg
    from full_sweep import upsert
    monkeypatch.setattr(pg, 'fetchrow', detail_db.fetchrow)
    monkeypatch.setattr(pg, 'execute', detail_db.execute)
    now = datetime.now(timezone.utc)
    source = {'id': '987654321', 'price': 30000000, 'title': '2-комнатная квартира',
              'area': 63, 'rooms': 2, 'address': 'Есильский р-н, Достык 1', 'floor': 8,
              '_source_details_checked_at': now.isoformat(), '_source_details_attempted_at': now.isoformat(),
              '_source_detail_values': {'area': 63, 'rooms': 2, 'address': 'Есильский р-н, Достык 1',
                                       'floor': 8, 'description': 'Наблюдаемое описание', 'furniture': False}}
    assert await upsert(source) == 'new'
    row = await detail_db.fetchrow("SELECT * FROM apartment_listings WHERE id='987654321'")
    assert row['rooms'] == 2 and row['area'] == 63 and row['floor'] == 8
    assert row['address'] == source['address'] and row['description'] == 'Наблюдаемое описание'
    assert row['source_details_checked_at'] == now and row['source_details_attempted_at'] == now


@pytest.mark.asyncio
async def test_real_existing_catalog_preserves_absent_fields_then_fresh_patch_updates_together(detail_db, monkeypatch):
    from bot.db import pg
    from full_sweep import upsert
    monkeypatch.setattr(pg, 'fetchrow', detail_db.fetchrow)
    monkeypatch.setattr(pg, 'execute', detail_db.execute)
    await detail_db.execute("UPDATE apartment_listings SET area=60,rooms=2,address='old address' WHERE id='123456789'")
    assert await upsert({'id': '123456789', 'price': 30000000, 'description': '', 'floor': None, 'details_fetched': False}) == 'upd'
    row = await detail_db.fetchrow("SELECT * FROM apartment_listings WHERE id='123456789'")
    assert row['description'] == 'old' and row['floor'] == 5 and row['year_built'] == 2000
    assert row['details_fetched'] is True and row['source_details_checked_at'] is None
    assert row['area'] == 60 and row['rooms'] == 2 and row['address'] == 'old address'
    now = datetime.now(timezone.utc)
    assert await upsert({'id': '123456789', 'price': 30000000,
                         '_source_details_checked_at': now.isoformat(),
                         '_source_detail_values': {'floor': 9, 'description': 'new', 'year_built': 2020}}) == 'upd'
    row = await detail_db.fetchrow("SELECT * FROM apartment_listings WHERE id='123456789'")
    assert row['floor'] == 9 and row['description'] == 'new' and row['year_built'] == 2020
    assert row['source_details_checked_at'] == now and row['source_details_attempted_at'] is None
