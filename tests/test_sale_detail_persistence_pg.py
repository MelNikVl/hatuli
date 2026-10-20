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
        await connection.execute('CREATE TEMP TABLE listing_complex_resolution_log '
                                 '(LIKE public.listing_complex_resolution_log INCLUDING DEFAULTS) ON COMMIT DROP')
        await connection.execute('SET LOCAL search_path=pg_temp,public')
        await connection.execute('ALTER TABLE apartment_listings ADD PRIMARY KEY(id)')
        await connection.execute("INSERT INTO apartment_listings(id,price,floor,floors_total,year_built,description,renovation,furniture,details_fetched) "
                                 "VALUES ('123456789',30000000,5,10,2000,'old','old','true',TRUE)")
        yield connection
    finally:
        await transaction.rollback()
        await connection.close()


async def bound_house(connection, method='house'):
    await connection.execute("""UPDATE apartment_listings SET
        complex_name='Проект',complex_url='https://krisha.kz/complex/show/astana/project/',
        address='Туран 6/1',description='корпус 1',lat=51.1,lon=71.2,
        resolved_house_id=21,house_attribution='address',house_attribution_detail='old proof',
        complex_id=21,complex_resolution=$1 WHERE id='123456789'""", method)


async def observed_patch(connection, values, *, checked=None, attempted=None):
    now = datetime.now(timezone.utc)
    return await save_source_details({'id':'123456789',
        '_source_details_checked_at': (checked or now).isoformat(),
        '_source_details_attempted_at': attempted.isoformat() if attempted else None,
        '_source_detail_values':values}, execute=connection.execute)


@pytest.mark.asyncio
@pytest.mark.parametrize('patch', [
    {'complex_name':'Другой проект'}, {'complex_url':'https://krisha.kz/complex/show/astana/other/'},
    {'address':'Туран 6/2'}, {'address':'Туран 6Б'}, {'description':'корпус 2'},
    {'lat':51.2}, {'lon':71.3}, {'complex_name':None},
])
async def test_fresh_changed_identity_clears_old_house_and_house_derived_fk_atomically(detail_db, patch):
    await bound_house(detail_db)
    assert await observed_patch(detail_db,patch) == 'UPDATE 1'
    row=await detail_db.fetchrow("SELECT * FROM apartment_listings WHERE id='123456789'")
    assert row['resolved_house_id'] is None and row['house_attribution'] is None
    assert row['house_attribution_detail'] is None
    assert row['complex_id'] is None and row['complex_resolution']=='unbound'
    assert row['source_details_checked_at'] is not None
    for field,value in patch.items():
        assert row[field]==pytest.approx(value) if isinstance(value,float) else row[field]==value
    log=await detail_db.fetchrow('SELECT * FROM listing_complex_resolution_log')
    assert log['previous_complex_id']==21 and log['complex_id'] is None
    assert log['previous_resolution_method']=='house' and log['resolution_method']=='unbound'
    assert log['resolver_version']=='sale_detail_invalidation_v1'
    await observed_patch(detail_db,patch)
    assert await detail_db.fetchval('SELECT count(*) FROM listing_complex_resolution_log')==1


@pytest.mark.asyncio
@pytest.mark.parametrize('patch', [{}, {'floor':8}, {'complex_name':'Проект'},
    {'address':'Туран 6/1'}, {'description':'корпус 1'}, {'lat':51.1,'lon':71.2}])
async def test_absent_or_unchanged_identity_does_not_invalidate_a_house(detail_db,patch):
    await bound_house(detail_db)
    await observed_patch(detail_db,patch)
    row=await detail_db.fetchrow("SELECT * FROM apartment_listings WHERE id='123456789'")
    assert row['resolved_house_id']==21 and row['complex_id']==21
    assert row['house_attribution_detail']=='old proof' and row['complex_resolution']=='house'
    assert await detail_db.fetchval('SELECT count(*) FROM listing_complex_resolution_log')==0


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['url','name'])
async def test_address_change_preserves_independent_complex_but_not_old_house(detail_db,method):
    await bound_house(detail_db,method)
    await observed_patch(detail_db,{'address':'Туран 6/2'})
    row=await detail_db.fetchrow("SELECT * FROM apartment_listings WHERE id='123456789'")
    assert row['complex_id']==21 and row['complex_resolution']==method
    assert row['resolved_house_id'] is None
    log=await detail_db.fetchrow('SELECT * FROM listing_complex_resolution_log')
    assert log['complex_id']==21 and log['previous_complex_id']==21
    assert log['resolution_method']==method


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['url','name','geo'])
async def test_new_source_complex_invalidates_every_old_binding_method(detail_db,method):
    await bound_house(detail_db,method)
    await observed_patch(detail_db,{'complex_name':'ЖК без известных детей'})
    row=await detail_db.fetchrow("SELECT * FROM apartment_listings WHERE id='123456789'")
    assert row['complex_id'] is None and row['resolved_house_id'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('attempt_only',[True,False])
async def test_failed_attempt_or_late_response_cannot_invalidate_identity(detail_db,attempt_only):
    await bound_house(detail_db)
    now=datetime.now(timezone.utc)
    await detail_db.execute("UPDATE apartment_listings SET source_details_checked_at=$1",now)
    await save_source_details({'id':'123456789',
        '_source_details_checked_at':None if attempt_only else (now-timedelta(hours=1)).isoformat(),
        '_source_details_attempted_at':now.isoformat(),
        '_source_detail_values':{'complex_name':'unconfirmed','address':'Туран 6/2'}},execute=detail_db.execute)
    row=await detail_db.fetchrow("SELECT * FROM apartment_listings WHERE id='123456789'")
    assert row['complex_id']==21 and row['resolved_house_id']==21 and row['complex_name']=='Проект'
    assert row['source_details_checked_at']==now
    assert await detail_db.fetchval('SELECT count(*) FROM listing_complex_resolution_log')==0


@pytest.mark.asyncio
async def test_log_failure_rolls_back_source_patch_and_invalidation_together(detail_db):
    await bound_house(detail_db)
    await detail_db.execute("ALTER TABLE listing_complex_resolution_log ADD CHECK (resolver_version <> 'sale_detail_invalidation_v1')")
    with pytest.raises(Exception):
        async with detail_db.transaction():
            await observed_patch(detail_db,{'complex_name':'Другой ЖК'})
    row=await detail_db.fetchrow("SELECT * FROM apartment_listings WHERE id='123456789'")
    assert row['complex_name']=='Проект' and row['resolved_house_id']==21 and row['complex_id']==21
    assert row['source_details_checked_at'] is None


@pytest.mark.asyncio
async def test_house_without_complex_fk_is_invalidated_and_logged(detail_db):
    await bound_house(detail_db)
    await detail_db.execute("UPDATE apartment_listings SET complex_id=NULL,complex_resolution=NULL")
    await observed_patch(detail_db,{'address':'Туран 6/2'})
    assert await detail_db.fetchval('SELECT resolved_house_id FROM apartment_listings') is None
    log=await detail_db.fetchrow('SELECT * FROM listing_complex_resolution_log')
    assert log['complex_id'] is None and log['confidence_tier']=='C'


@pytest.mark.asyncio
async def test_old_inflight_derivation_cannot_restore_house_after_new_source_patch(detail_db):
    await bound_house(detail_db)
    old_time=datetime.now(timezone.utc)-timedelta(seconds=2)
    await detail_db.execute('UPDATE apartment_listings SET source_details_checked_at=$1',old_time)
    old_row=await detail_db.fetchrow('SELECT source_details_checked_at,last_seen FROM apartment_listings')
    await observed_patch(detail_db,{'complex_name':'Другой проект'})
    # The existing collector guards the derived house write with its saved
    # source snapshot. A resolver finishing later must not restore old proof.
    status=await detail_db.execute("""UPDATE apartment_listings SET resolved_house_id=$2,
        house_attribution=$3,house_attribution_detail=$4 WHERE id=$1
        AND source_details_checked_at IS NOT DISTINCT FROM $5::timestamptz
        AND last_seen IS NOT DISTINCT FROM $6::timestamptz""",'123456789',21,'address','old',
        old_row['source_details_checked_at'],old_row['last_seen'])
    assert status=='UPDATE 0'
    assert await detail_db.fetchval('SELECT resolved_house_id FROM apartment_listings') is None


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
async def test_real_pg_geo_provenance_requires_fresh_observed_coordinate_pair(detail_db):
    now = datetime.now(timezone.utc)
    await detail_db.execute("UPDATE apartment_listings SET lat=51,lon=71,geo_source='address' WHERE id='123456789'")
    await save_source_details({'id': '123456789', '_source_details_checked_at': now.isoformat(),
                               '_source_detail_values': {'lat': 51.1}}, execute=detail_db.execute)
    assert await detail_db.fetchval("SELECT geo_source FROM apartment_listings WHERE id='123456789'") == 'address'
    await save_source_details({'id': '123456789', '_source_details_checked_at': now.isoformat(),
                               '_source_detail_values': {'lat': 51.2, 'lon': 71.2}}, execute=detail_db.execute)
    row = await detail_db.fetchrow("SELECT lat,lon,geo_source FROM apartment_listings WHERE id='123456789'")
    assert row['lat'] == pytest.approx(51.2) and row['lon'] == pytest.approx(71.2) and row['geo_source'] == 'krisha'
    await save_source_details({'id': '123456789', '_source_details_checked_at': (now-timedelta(hours=1)).isoformat(),
                               '_source_detail_values': {'lat': 52, 'lon': 72}}, execute=detail_db.execute)
    assert dict(await detail_db.fetchrow("SELECT lat,lon,geo_source FROM apartment_listings WHERE id='123456789'")) == dict(row)


@pytest.mark.asyncio
async def test_real_pg_raw_source_complex_fields_are_monotonic_and_absence_preserves(detail_db):
    now = datetime.now(timezone.utc)
    fresh = {'complex_name': 'ЖК Источник', 'complex_url': 'https://krisha.kz/complex/show/source-123/'}
    await save_source_details({'id': '123456789', '_source_details_checked_at': now.isoformat(),
                               '_source_detail_values': fresh}, execute=detail_db.execute)
    await save_source_details({'id': '123456789', '_source_details_checked_at': (now-timedelta(hours=1)).isoformat(),
                               '_source_detail_values': {'complex_name': 'Старый ЖК', 'complex_url': 'stale'}},
                              execute=detail_db.execute)
    await save_source_details({'id': '123456789', '_source_details_checked_at': (now+timedelta(seconds=1)).isoformat(),
                               '_source_detail_values': {'floor': 7}}, execute=detail_db.execute)
    assert dict(await detail_db.fetchrow("SELECT complex_name,complex_url FROM apartment_listings WHERE id='123456789'")) == fresh


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
