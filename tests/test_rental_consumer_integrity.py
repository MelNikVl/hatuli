"""Rental failures cannot become fake departures or fresh rental estimates."""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date,datetime,timedelta,timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from bot.core.rental_data_quality import collection_health,rental_clean_sql,rental_live_sql,rental_confirmed_archive_sql
from scripts.audit_rental_coverage import audit,artifact_projection,setting_projection,QUERIES

NOW = datetime(2026,10,13,12,tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def prevent_real_database(monkeypatch):
    async def reject(*args,**kwargs):
        pytest.fail('Rental consumer tests must never open PostgreSQL')
    monkeypatch.setattr('asyncpg.connect',reject)
    monkeypatch.setattr('asyncpg.create_pool',reject)
    for name in ('fetch','fetchval','fetchrow','execute'):
        monkeypatch.setattr('bot.db.pg.'+name,reject)


def test_real_scope_excludes_old_archived_duplicate_disputed_and_other_type_rows():
    db = sqlite3.connect(':memory:')
    db.create_function('btrim',1,lambda value: value.strip() if isinstance(value,str) else None)
    db.execute('''CREATE TABLE rental_listings(id text,prop_type text,city text,is_active bool,
        archived_at text,last_seen text,is_duplicate bool,dup_needs_review bool,duplicate_of text,archive_reason text,
        rent_period text DEFAULT 'monthly',rent_period_conflict bool DEFAULT FALSE)''')
    base = ('apartment','astana',1,None,'2026-10-13 10:00:00',0,0,None,None)
    rows = [
        ('live',*base),
        ('overdue',*base[:4],'2026-10-01 10:00:00',*base[5:]),
        ('archived','apartment','astana',0,'2026-10-12 10:00:00','2026-10-12 09:00:00',0,0,None,'archived_badge'),
        ('unknown-archive','apartment','astana',0,'2026-10-12 10:00:00','2026-10-12 09:00:00',0,0,None,None),
        ('stale-archive','apartment','astana',0,'2026-10-12 10:00:00','2026-10-12 09:00:00',0,0,None,'sweep_stale'),
        ('duplicate',*base[:5],1,*base[6:]),
        ('disputed',*base[:6],1,*base[7:]),
        ('duplicate-of',*base[:7],'live',base[8]),
        ('parking','parking',*base[1:]),
        ('other-city','apartment','almaty',*base[2:]),
    ]
    db.executemany('INSERT INTO rental_listings(id,prop_type,city,is_active,archived_at,last_seen,is_duplicate,dup_needs_review,duplicate_of,archive_reason) VALUES (?,?,?,?,?,?,?,?,?,?)',rows)
    live_sql = rental_live_sql().replace("now() - interval '72 hours'","'2026-10-10 12:00:00'").replace('now()',"'2026-10-13 12:00:00'")
    assert [row[0] for row in db.execute('SELECT id FROM rental_listings WHERE '+live_sql)] == ['live']
    assert [row[0] for row in db.execute('SELECT id FROM rental_listings WHERE '+rental_confirmed_archive_sql())] == ['archived']
    db.close()


@pytest.mark.parametrize('alias',['r; DROP TABLE rental_listings','r.x','r--','1r'])
def test_scope_rejects_sql_alias_injection(alias):
    with pytest.raises(ValueError):
        rental_clean_sql(alias)


@pytest.mark.parametrize('age',[0,-1,True,1.5,721,'72'])
def test_scope_cannot_interpolate_unapproved_interval(age):
    with pytest.raises(ValueError):
        rental_live_sql(max_age_hours=age)


@pytest.mark.parametrize('last',[None,NOW-timedelta(days=5),NOW+timedelta(minutes=1),NOW.replace(tzinfo=None)])
def test_missing_old_or_future_observation_does_not_claim_recent_collection(last):
    state = collection_health({},last,now=NOW)
    assert state['collection_status']!='recent'
    assert state['market_coverage']=='unknown'
    assert 'не означает' in state['note']


@pytest.mark.parametrize('duration',['nan','inf','-1','0','broken','180224'])
def test_recent_single_page_is_not_a_healthy_complete_sweep(duration):
    state = collection_health({'RENTAL_CRAWL_V1_APARTMENT_COMPLETED_AT':NOW.isoformat(),
                               'RENTAL_CRAWL_V1_APARTMENT_DURATION_SEC':duration},NOW,now=NOW)
    assert state['collection_status']=='stale'
    assert state['market_coverage']=='unknown'


def test_recent_sweep_still_does_not_assert_complete_market():
    state = collection_health({'RENTAL_CRAWL_V1_APARTMENT_COMPLETED_AT':NOW.isoformat(),
                               'RENTAL_CRAWL_V1_APARTMENT_DURATION_SEC':'3600',
                               'RENTAL_CRAWL_V1_APARTMENT_COMPLETED_VERIFIED':'1',
                               'RENTAL_CRAWL_V1_APARTMENT_LAST_PAGES':'5',
                               'RENTAL_CRAWL_V1_APARTMENT_UNIQUE_SEEN':'96',
                               'RENTAL_CRAWL_V1_APARTMENT_TOTAL':'100'},NOW,now=NOW)
    assert state['collection_status']=='recent'
    assert state['market_coverage']=='unknown'
    assert 'не подтверждённая сдача' in state['archive_note']


@pytest.mark.parametrize('proof_change',[
    {'RENTAL_CRAWL_V1_APARTMENT_COMPLETED_VERIFIED':'0'},
    {'RENTAL_CRAWL_V1_APARTMENT_COMPLETED_VERIFIED':None},
    {'RENTAL_CRAWL_V1_APARTMENT_UNIQUE_SEEN':'94'},
    {'RENTAL_CRAWL_V1_APARTMENT_TOTAL':'0'},
    {'RENTAL_CRAWL_V1_APARTMENT_LAST_PAGES':'bad'},
])
def test_legacy_or_incomplete_completion_cannot_turn_rental_health_recent(proof_change):
    settings = {'RENTAL_CRAWL_V1_APARTMENT_COMPLETED_AT':NOW.isoformat(),
                'RENTAL_CRAWL_V1_APARTMENT_DURATION_SEC':'3600',
                'RENTAL_CRAWL_V1_APARTMENT_COMPLETED_VERIFIED':'1',
                'RENTAL_CRAWL_V1_APARTMENT_LAST_PAGES':'5',
                'RENTAL_CRAWL_V1_APARTMENT_UNIQUE_SEEN':'96',
                'RENTAL_CRAWL_V1_APARTMENT_TOTAL':'100',**proof_change}
    assert collection_health(settings,NOW,now=NOW)['collection_status']=='stale'


@pytest_asyncio.fixture
async def client(monkeypatch):
    from bot.core import site_auth
    import terminal_extras
    async def tier(request):
        return getattr(request.state,'test_role','public')
    monkeypatch.setattr(site_auth,'get_user_tier',tier)
    app = FastAPI()
    @app.middleware('http')
    async def auth(request,call_next):
        role = request.headers.get('x-test-role','public')
        request.state.test_role = role
        request.state.admin_user = {'id':42,'username':'rental-qa'} if role=='admin' else None
        return await call_next(request)
    app.include_router(terminal_extras.make_extras_router(SimpleNamespace(env=SimpleNamespace(globals={}))))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://test') as connection:
        yield connection


def mock_health(monkeypatch):
    async def health():
        return collection_health({},NOW-timedelta(days=5),now=NOW)
    monkeypatch.setattr('terminal_extras.rental_consumer_health',health)


@pytest.mark.asyncio
async def test_archive_history_uses_explicit_confirmation_and_explains_collection_gap(client,monkeypatch):
    mock_health(monkeypatch)
    queries = []
    async def fetch(query,*args):
        queries.append(query)
        if 'FROM rental_listings' in query:
            assert 'archived_at::date' in query
            assert "prop_type = 'apartment'" in query
            assert 'is_active IS FALSE' in query
            assert "archive_reason IN ('archived_badge', 'confirmed_gone')" in query
            assert 'dup_needs_review' in query and 'duplicate_of' in query
            assert "last_seen <" not in query and "interval '3 days'" not in query
            return [{'d':date(2026,10,12),'room_bucket':2,'cnt':3}]
        return []
    monkeypatch.setattr('bot.db.pg.fetch',fetch)
    response = await client.get('/admin/api/archived-history',headers={'x-test-role':'admin'})
    assert response.status_code==200
    result = response.json()
    assert result['series']['rental_2']==[3]
    assert result['rental_data_quality']['collection_status']=='stale'
    assert result['rental_data_quality']['market_coverage']=='unknown'
    assert len(queries)==2


@pytest.mark.asyncio
async def test_confirmed_archive_hex_uses_confirmation_time_not_last_seen(client,monkeypatch):
    async def fetch(query,*args):
        assert 'archived_at - found_at' in query
        assert 'is_active IS FALSE' in query
        assert 'last_seen <' not in query
        assert 'dup_needs_review' in query
        return []
    monkeypatch.setattr('bot.db.pg.fetch',fetch)
    response = await client.get('/admin/api/archived-hex-points?type=rental',headers={'x-test-role':'admin'})
    assert response.status_code==200 and response.json()['points']==[]


@pytest.mark.asyncio
@pytest.mark.parametrize('path',['/admin/api/geo-rentals.geojson','/admin/api/geo-kepler.json?type=rental'])
async def test_geo_exports_do_not_mix_old_or_archived_market_into_live_rentals(client,monkeypatch,path):
    calls = []
    async def fetch(query,*args):
        calls.append(query)
        assert 'is_active IS TRUE' in query and 'archived_at IS NULL' in query
        assert "interval '72 hours'" in query and 'dup_needs_review' in query
        assert "prop_type = 'apartment'" in query
        return []
    monkeypatch.setattr('bot.db.pg.fetch',fetch)
    response = await client.get(path,headers={'x-test-role':'admin'})
    assert response.status_code==200
    assert len(calls)==1


@pytest.mark.asyncio
async def test_rental_map_uses_fresh_live_rows_and_catalog_cover_without_gallery(client,monkeypatch):
    cover='https://astps-photos-kr.kcdn.kz/webp/56/56e36366-f634-480b-be27-f81efd89f4b1/4-400x300.jpg'
    async def fetch(query,*args):
        if 'FROM rental_listings r' in query:
            assert 'is_active IS TRUE' in query and 'dup_needs_review' in query
            assert "interval '72 hours'" in query and "prop_type = 'apartment'" in query
            assert 'r.photo_url' in query and 'archived_at >' not in query
            return [{'id':'12345678','url':'https://krisha.kz/a/show/12345678','price':250000,
                'rooms':2,'area':60,'floor':3,'floors_total':10,'address':'QA',
                'complex_name':None,'complex_key':'','district':'Есиль','found_at':NOW,
                'own_lat':51.1,'own_lon':71.4,'photos':None,'photo_url':cover,
                'prev_price':None,'price_changed_at':None}]
        return []
    monkeypatch.setattr('bot.db.pg.fetch',fetch)
    response=await client.get('/admin/api/map-points?type=rental',headers={'x-test-role':'admin'})
    assert response.status_code==200
    result=response.json()
    assert result['points'][0]['photos']==[cover]
    assert result['market_coverage']=='unknown' and result['limited_to']==1000


@pytest.mark.asyncio
async def test_archive_endpoints_still_deny_public_without_querying_database(client):
    for path in ('/admin/api/archived-history','/admin/api/archived-hex-points?type=rental'):
        response = await client.get(path)
        assert response.status_code==401


@pytest.mark.asyncio
@pytest.mark.parametrize('path',[
    '/admin/api/complex/10/turnover-dynamics?kind=rental',
    '/admin/api/complex/10/price-dynamics?kind=rental',
])
async def test_complex_rental_charts_have_honest_scopes_and_health(client,monkeypatch,path):
    mock_health(monkeypatch)
    async def fetchrow(query,*args):
        return {'id':10,'name':'Синтетический ЖК','is_umbrella':False}
    async def fetchval(query,*args):
        return False
    async def fetch(query,*args):
        assert 'dup_needs_review' in query and 'duplicate_of' in query
        assert "prop_type = 'apartment'" in query
        if 'turnover' in path:
            assert 'archived_at - found_at' in query
            assert 'is_active IS FALSE' in query and 'archive_reason' in query
            assert "interval '3 days'" not in query
            return [{'d':date(2026,10,12),'rooms':2,'avg_days':11.3,'n':3}]
        return [{'d':date(2026,10,12),'rooms':2,'median_price':300000,'n':3}]
    monkeypatch.setattr('bot.db.pg.fetchrow',fetchrow)
    monkeypatch.setattr('bot.db.pg.fetchval',fetchval)
    monkeypatch.setattr('bot.db.pg.fetch',fetch)
    response = await client.get(path)
    assert response.status_code==200
    result = response.json()
    assert result['data_quality']['collection_status']=='stale'
    assert result['data_quality']['market_coverage']=='unknown'
    assert result['data']['2'][0]['n']==3
    if 'price-dynamics' in path:
        assert 'Историческая цена рынка' in result['series_note']


def test_audit_settings_projection_hides_unapproved_raw_cursor_fields():
    result = setting_projection([
        {'key':'RENTAL_CRAWL_V1_APARTMENT','value':json.dumps({'page':9,'signatures':['private-hash'],'secret':'PRIVATE'})},
        {'key':'RENTAL_CRAWL_V1_API_KEY','value':'PRIVATE'},
        {'key':'SHEETS_RENTAL_SYNCED_AT','value':'2026-10-13T12:00:00+00:00'},
    ])
    assert result['RENTAL_CRAWL_V1_APARTMENT']=={'page':9,'seen_page_signatures':1}
    assert 'PRIVATE' not in json.dumps(result)


def test_audit_snapshot_never_exports_cards_addresses_or_photos(tmp_path):
    (tmp_path/'var').mkdir()
    (tmp_path/'var/market-comparison.json').write_text(json.dumps({'schema_version':3,'offers':[
        {'id':'12345','address':'PRIVATE','photos':['PRIVATE'],'rental_estimate':{'sample_count':8}}]}),encoding='utf-8')
    result = artifact_projection(tmp_path)
    assert result['offers']==1 and result['with_rental_estimate']==1
    assert 'PRIVATE' not in json.dumps(result)


class FakeConnection:
    def __init__(self,readonly):
        self.readonly = readonly
        self.queries = []
        self.closed = False
    def transaction(self,**options):
        self.options = options
        return self
    async def __aenter__(self):
        return self
    async def __aexit__(self,*args):
        pass
    async def fetchval(self,query):
        return self.readonly if query.startswith('SHOW') else NOW
    async def fetch(self,query,*args):
        self.queries.append(query)
        return []
    async def close(self):
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize('readonly',['on','off'])
async def test_audit_connection_and_transaction_are_read_only_and_fail_closed(monkeypatch,tmp_path,readonly):
    conn = FakeConnection(readonly)
    options = {}
    async def connect(dsn,**kwargs):
        options.update(kwargs)
        return conn
    monkeypatch.setitem(sys.modules,'asyncpg',SimpleNamespace(connect=connect))
    if readonly=='off':
        with pytest.raises(RuntimeError,match='read-only'):
            await audit('dummy-dsn',tmp_path)
        assert conn.queries==[]
    else:
        report = await audit('dummy-dsn',tmp_path)
        assert report['database_writes']==0
        assert report['market_completeness']=='unknown_without_independent_source_enumeration'
        assert all(query.strip().startswith(('SELECT','WITH')) for query in conn.queries)
        assert len(conn.queries)==len(QUERIES)+2
    assert options['server_settings']['default_transaction_read_only']=='on'
    assert conn.options=={'readonly':True,'isolation':'repeatable_read'}
    assert conn.closed is True
