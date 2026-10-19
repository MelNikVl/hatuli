"""Actual consumer scopes never treat daily/unknown prices as monthly rents."""
from datetime import datetime, timedelta, timezone
import re
import sqlite3
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from bot.core.rental_data_quality import rental_clean_sql, rental_live_sql, rental_confirmed_archive_sql
from bot.core.sheets_sync_rental import RENTAL_EXPORT_SQL, rental_sheet_rows
from bot.rental_detail_web import project_rental_detail, DETAIL_SQL

NOW = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setenv('DATABASE_URL', 'postgresql://unused:unused@127.0.0.1:1/rental_period_test')
    monkeypatch.setattr('dotenv.load_dotenv', lambda *args, **kwargs: False)
    async def reject(*args, **kwargs):
        pytest.fail('Consumer tests must never connect to PostgreSQL')
    for path in ('asyncpg.connect', 'asyncpg.create_pool', 'bot.db.pg.init_pool', 'bot.db.pg.get_pool',
                 'bot.db.pg.fetch', 'bot.db.pg.fetchrow', 'bot.db.pg.fetchval', 'bot.db.pg.execute'):
        monkeypatch.setattr(path, reject)


def sqlite_sql(sql):
    def interval(match):
        age = timedelta(**{match[2]: int(match[1])})
        return "'" + (NOW-age).strftime('%Y-%m-%d %H:%M:%S') + "'"
    sql = re.sub(r"now\(\)\s*-\s*interval '([0-9]+) (hours|days)'", interval, sql)
    return sql.replace('now()', "'2026-10-15 12:00:00'")


@pytest.fixture
def inventory():
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.create_function('btrim', 1, lambda value: value.strip() if isinstance(value, str) else None)
    db.execute('''CREATE TABLE rental_listings(id text, prop_type text, city text,
        rent_period text, rent_period_conflict bool, is_active bool, archived_at text,
        archive_reason text, last_seen text, is_duplicate bool DEFAULT FALSE,
        dup_needs_review bool DEFAULT FALSE, duplicate_of text, price int DEFAULT 250000,
        rooms int DEFAULT 2, district text, complex_name text, area real DEFAULT 60,
        floor int, floors_total int, address text, url text, lat real DEFAULT 51.1,
        lon real DEFAULT 71.4)''')
    for period in ('monthly', 'daily', 'unknown', None):
        name = period or 'missing'
        db.execute('''INSERT INTO rental_listings(id,prop_type,city,rent_period,rent_period_conflict,
            is_active,last_seen) VALUES(?,?,?,?,FALSE,TRUE,?)''',
            (name, 'apartment', 'astana', period, '2026-10-15 10:00:00'))
        db.execute('''INSERT INTO rental_listings(id,prop_type,city,rent_period,rent_period_conflict,
            is_active,archived_at,archive_reason,last_seen) VALUES(?,?,?,?,FALSE,FALSE,?,?,?)''',
            (name+'-archive', 'apartment', 'astana', period, '2026-10-14 11:00:00',
             'archived_badge', '2026-10-14 10:00:00'))
    for kind in ('parking', 'commercial'):
        db.execute('''INSERT INTO rental_listings(id,prop_type,city,rent_period,rent_period_conflict,
            is_active,last_seen) VALUES(?,?,?,'unknown',FALSE,TRUE,?)''',
            (kind, kind, 'astana', '2026-10-15 10:00:00'))
    db.execute("INSERT INTO rental_listings(id,prop_type,city,rent_period,rent_period_conflict,is_active,last_seen) VALUES('conflict','apartment','astana','monthly',TRUE,TRUE,'2026-10-15 10:00:00')")
    yield db
    db.close()


def test_monthly_scopes_execute_against_mixed_period_rows(inventory):
    def ids(scope):
        return {r['id'] for r in inventory.execute('SELECT id FROM rental_listings WHERE '+sqlite_sql(scope))}
    assert ids(rental_clean_sql()) == {'monthly', 'monthly-archive'}
    assert ids(rental_live_sql()) == {'monthly'}
    assert ids(rental_confirmed_archive_sql()) == {'monthly-archive'}


def test_sheet_query_keeps_other_types_without_mislabeling_unknown_price_units(inventory):
    rows = [dict(r) for r in inventory.execute(sqlite_sql(RENTAL_EXPORT_SQL))]
    assert {r['id'] for r in rows} == {'monthly', 'parking', 'commercial'}
    for row in rows:
        row['last_seen'] = datetime.fromisoformat(row['last_seen'])
    displayed = {r[0]: r for r in rental_sheet_rows(rows)[1:]}
    assert displayed['monthly'][7] == 'месяц'
    assert displayed['parking'][7] == displayed['commercial'][7] == 'не определена'


@pytest.mark.asyncio
async def test_daily_observation_cannot_make_stale_monthly_collection_look_fresh(monkeypatch,inventory):
    from bot.core.rental_data_quality import rental_consumer_health
    inventory.execute("UPDATE rental_listings SET last_seen='2026-10-01 10:00:00' WHERE id='monthly'")
    async def fetch(sql,*args):
        return []
    async def latest(sql,*args):
        observed = inventory.execute(sql).fetchone()[0]
        return datetime.fromisoformat(observed).replace(tzinfo=timezone.utc)
    monkeypatch.setattr('bot.db.pg.fetch',fetch)
    monkeypatch.setattr('bot.db.pg.fetchval',latest)
    result = await rental_consumer_health()
    assert result['last_observed_at'].startswith('2026-10-01')
    assert result['collection_status']=='stale'


@pytest.mark.parametrize('period,conflict,kind,want', [
    ('monthly',False,'apartment','month'), ('daily',False,'apartment','day'),
    ('unknown',False,'apartment','unknown'), (None,False,'apartment','unknown'),
    ('monthly',True,'apartment','unknown'), ('daily',True,'apartment','unknown'),
    ('monthly',False,'parking','unknown'), ('monthly',False,'commercial','unknown'),
])
def test_detail_period_is_explicit_and_unknown_does_not_inherit_monthly(period,conflict,kind,want):
    data = project_rental_detail({'id':'1234567','prop_type':kind,'rent_period':period,
        'rent_period_conflict':conflict,'price':20000,'is_active':True,'last_seen':NOW}, [], now=NOW)
    assert data['price_unit'] == want
    assert data['monthly_category'] is (kind=='apartment' and want=='month')
    assert data['rent_period'] == ('unknown' if conflict or period is None else period)
    assert 'rent_period' in DETAIL_SQL and 'rent_period_conflict' in DETAIL_SQL


@pytest_asyncio.fixture
async def client(monkeypatch):
    from bot.core import site_auth
    import terminal_extras
    async def tier(request):
        return 'admin'
    monkeypatch.setattr(site_auth, 'get_user_tier', tier)
    app = FastAPI()
    @app.middleware('http')
    async def authenticated(request, call_next):
        request.state.admin_user = {'id':1,'username':'test'}
        return await call_next(request)
    app.include_router(terminal_extras.make_extras_router(SimpleNamespace(env=SimpleNamespace(globals={}))))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://test') as connection:
        yield connection


@pytest.mark.asyncio
@pytest.mark.parametrize('path', [
    '/admin/api/geo-rentals.geojson', '/admin/api/geo-kepler.json?type=rental',
    '/admin/api/map-points-lite?type=rental',
])
async def test_actual_geo_and_kepler_queries_exclude_daily_unknown_and_conflicting_rows(client,monkeypatch,inventory,path):
    queries = []
    async def fetch(sql,*args):
        queries.append(sql)
        assert "rent_period = 'monthly'" in sql and 'rent_period_conflict' in sql
        return [dict(r) for r in inventory.execute(sqlite_sql(sql))]
    monkeypatch.setattr('bot.db.pg.fetch', fetch)
    response = await client.get(path)
    assert response.status_code == 200
    assert len(queries) == 1
    data = response.json()
    if 'features' in data:
        assert len(data['features']) == 1 and data['features'][0]['properties']['id']=='monthly'
    elif 'datasets' in data:
        assert len(data['datasets'][0]['data']['allData']) == 1
    else:
        assert len(data['points']) == 1


@pytest.mark.asyncio
async def test_monthly_coverage_never_archives_daily_unknown_or_conflicting_rows(monkeypatch,inventory):
    from bot.core import stale_archive
    inventory.execute("UPDATE rental_listings SET last_seen='2026-10-01 10:00:00' WHERE is_active IS TRUE")
    prefix = stale_archive.RENTAL_PREFIX
    proof = {'COMPLETED_AT':NOW.isoformat(),'DURATION_SEC':'86400','COMPLETED_VERIFIED':'1',
             'LAST_PAGES':'200','UNIQUE_SEEN':'4000','TOTAL':'4000','LAST_SUCCESS_AT':NOW.isoformat()}
    async def fetch(sql,*args):
        return [{'key':prefix+'_'+key,'value':value} for key,value in proof.items()]
    async def count(sql,cutoff):
        query = sql.replace('$1','?')
        return inventory.execute(query,(cutoff.strftime('%Y-%m-%d %H:%M:%S'),)).fetchone()[0]
    async def execute(sql,cutoff,reason):
        query = sql.replace('$1','?1').replace('$2','?2')
        changed = inventory.execute(query,(cutoff.strftime('%Y-%m-%d %H:%M:%S'),reason)).rowcount
        return f'UPDATE {changed}'
    monkeypatch.setattr('bot.db.pg.fetch',fetch)
    monkeypatch.setattr('bot.db.pg.fetchval',count)
    monkeypatch.setattr('bot.db.pg.execute',execute)
    result = await stale_archive.archive_stale_rentals(now=NOW)
    assert result['healthy'] and result['candidates']==result['archived']==1
    assert {r['id'] for r in inventory.execute('SELECT id FROM rental_listings WHERE is_active IS TRUE')} == {
        'daily','unknown','missing','conflict','parking','commercial'}


def test_complex_monthly_counts_are_period_separated():
    from bot.core.complex_metrics import CURRENT_METRICS_SQL
    assert "r.rent_period = 'monthly'" in CURRENT_METRICS_SQL
    assert 'NOT COALESCE(r.rent_period_conflict, FALSE)' in CURRENT_METRICS_SQL
