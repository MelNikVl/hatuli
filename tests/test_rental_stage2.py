"""Synthetic unit cases only. No assertion here is a 50-real-source audit."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import hashlib
import json

import asyncpg
import httpx
import pytest
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from jinja2 import Environment,FileSystemLoader,select_autoescape

from bot.core import rental_furnishing as furniture,rental_stage2_index as index,rental_parser as parser
from bot import rental_analytics_web as web
from bot.db import pg,settings
from scripts.audit_rental_furnishing import dataset_template,evaluate_dataset,screenshot_check

NOW=datetime(2026,10,16,12,tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setenv('DATABASE_URL','postgresql://unused:unused@127.0.0.1:1/rental_stage2_unit')
    monkeypatch.setattr('dotenv.load_dotenv',lambda *a,**k:False)
    async def deny(*args,**kwargs):
        raise AssertionError('No database, pool initialization or source HTTP in stage2 unit tests')
    monkeypatch.setattr(asyncpg,'connect',deny)
    monkeypatch.setattr(asyncpg,'create_pool',deny)
    monkeypatch.setattr(pg,'init_pool',deny)
    for name in ('fetch','fetchrow','fetchval','execute'):
        monkeypatch.setattr(pg,name,deny)
    monkeypatch.setattr(settings,'get',lambda key,default=None:default)
    monkeypatch.setattr(settings,'get_int',lambda key,default=0:default)


def source(identifier=1,period='daily',**changes):
    return {'id':str(identifier),'city':'astana','district':'Есиль','rooms':2,'prop_type':'apartment',
            'rent_period':period,'period_source_checked':True,'price':20000 if period=='daily' else 250000,'area':50.,
            'observed_at':NOW-timedelta(hours=1),'last_seen':NOW-timedelta(hours=1),'is_active':True,
            'archived_at':None,'is_duplicate':False,'dup_needs_review':False,'duplicate_of':None,
            'rent_period_conflict':False,'complex_binding_verified':True,'index_complex_id':10,
            'index_complex_name':'Канонический ЖК','hex_id':'0:0','hex_edge_m':50.,
            'furnishing':'full','furnishing_version':furniture.version_tag(),**changes}


def records(history=None,live=None,removed=None,**changes):
    return index.build_stage2_records(history or [],live or [],removed or [],NOW,**changes)


def exact(rows,scope='complex',period='daily',furnishing=None):
    return next(row for row in rows if row['scope']==scope and row['rent_period']==period and row['furnishing']==furnishing)


def aggregate(period='daily',**changes):
    return {'city':'astana','district':'Есиль','complex_id':None,'complex_name':None,'rooms':2,
            'rent_period':period,'furnishing':None,'median_price':20000 if period=='daily' else 250000,
            'p25_price':18000 if period=='daily' else 230000,'p75_price':24000 if period=='daily' else 280000,
            'sample_count':10,'active_supply':7,'window_days':30,'updated_at':NOW-timedelta(minutes=1),
            'source_oldest_at':NOW-timedelta(hours=3),'source_newest_at':NOW-timedelta(hours=1),
            'policy_version':index.DAILY_POLICY if period=='daily' else '3',**changes}


def test_daily_prefers_30_days_and_each_listing_has_one_vote():
    rows=[source(i,price=10000+i*1000) for i in range(5)]
    rows += [source(0,price=1000000,observed_at=NOW-timedelta(days=10))]*10
    value=exact(records(rows,rows[:5]))
    assert value['sample_count']==5 and value['window_days']==30
    assert value['median_price']==12000 and value['p25_price']==11000 and value['p75_price']==13000
    assert value['active_supply']==5


def test_daily_current_catalog_price_replaces_first_day_snapshot_once_per_id():
    history=[source(i,price=20000) for i in range(5)]
    live=[source(i,price=30000,rental_episode_started_at=NOW-timedelta(hours=2),
                 rental_episode_last_seen_at=NOW-timedelta(minutes=1),last_seen=NOW) for i in range(5)]
    result=exact(records(history*3,live))
    assert result['median_price']==30000 and result['sample_count']==5 and result['active_supply']==5
    assert result['source_newest_at']==NOW-timedelta(minutes=1)
    assert all(row['price']==20000 for row in history)


@pytest.mark.parametrize('proof',[
    {},{'rental_episode_started_at':NOW-timedelta(days=10),'rental_episode_last_seen_at':NOW-timedelta(hours=73)},
    {'rental_episode_started_at':NOW,'rental_episode_last_seen_at':NOW-timedelta(hours=1)},
    {'rental_episode_started_at':NOW,'rental_episode_last_seen_at':NOW+timedelta(seconds=1)},
])
def test_daily_generic_freshness_cannot_replace_catalog_proof(proof):
    history=[source(i,price=20000) for i in range(5)]
    live=[source(i,price=30000,last_seen=NOW,**proof) for i in range(5)]
    assert exact(records(history,live))['median_price']==20000


def test_daily_latest_catalog_scope_does_not_leave_old_room_group():
    history=[source(i,rooms=1) for i in range(5)]
    live=[source(i,rooms=2,rental_episode_started_at=NOW-timedelta(hours=2),
                 rental_episode_last_seen_at=NOW-timedelta(minutes=1),last_seen=NOW) for i in range(5)]
    result=records(history,live)
    assert result and all(row['rooms']==2 for row in result)


def test_daily_90_day_fallback_requires_actual_old_observations():
    rows=[source(i) for i in range(4)]+[source(4,observed_at=NOW-timedelta(days=45))]
    value=exact(records(rows,[source(0)]))
    assert value['window_days']==90 and value['sample_count']==5 and value['active_supply']==1
    assert value['source_oldest_at']==NOW-timedelta(days=45)
    assert not records([],rows)  # current rows cannot manufacture history
    assert not records(rows[:-1],[source(0)])


@pytest.mark.parametrize('changes',[
    {'rent_period':'monthly'},{'rent_period':'unknown'},{'rent_period_conflict':True},
    {'is_duplicate':True},{'dup_needs_review':True},{'duplicate_of':'other'},
    {'period_source_checked':False},{'rooms':0},{'price':True},{'price':None},
    {'observed_at':NOW+timedelta(seconds=1)},{'observed_at':NOW-timedelta(days=91)},
    {'city':'almaty'},
])
def test_daily_cannot_reach_threshold_using_bad_observations(changes):
    rows=[source(i) for i in range(4)]+[source(4,**changes)]
    assert not records(rows,[source(0)])


def test_old_history_and_fresh_last_seen_do_not_fabricate_fresh_daily_prices():
    rows=[source(i,observed_at=NOW-timedelta(days=4)) for i in range(5)]
    assert not records(rows,[source(0,last_seen=NOW)])
    fresh=[source(i) for i in range(5)]
    assert not records(fresh,[source(0,last_seen=NOW-timedelta(hours=73))])


def test_area_needs_ten_and_hex_needs_five_and_matching_grid():
    rows=[source(i) for i in range(9)]
    result=records(rows,rows)
    assert not any(row['scope']=='district' for row in result)
    assert exact(result,'hex')['sample_count']==9
    rows.append(source(9))
    assert exact(records(rows,rows),'district')['sample_count']==10
    split=[source(i,hex_edge_m=100. if i==4 else 50.) for i in range(5)]
    assert not any(row['scope']=='hex' for row in records(split,split))


def test_unverified_or_umbrella_identity_never_receives_complex_index():
    rows=[source(i,complex_binding_verified=False,index_complex_id=10) for i in range(10)]
    assert not any(row['scope']=='complex' for row in records(rows,rows))
    assert exact(records(rows,rows),'district')['sample_count']==10


def test_monthly_never_uses_daily_history_or_stale_live_prices():
    live=[source(i,'monthly') for i in range(5)]
    result=records([source(i,price=1000) for i in range(50)],live)
    assert exact(result,period='monthly')['median_price']==250000
    assert not any(row['rent_period']=='daily' for row in result)
    assert not records([], [source(i,'monthly',last_seen=NOW-timedelta(hours=73)) for i in range(5)])
    assert not records([], [source(i,'monthly',title='Квартира посуточно') for i in range(5)])


def test_furnished_groups_are_disabled_until_real_validation_and_version_match():
    rows=[source(i) for i in range(5)]
    assert all(row['furnishing'] is None for row in records(rows,rows))
    result=records(rows,rows,furnishing_enabled=True,furnishing_version=furniture.version_tag())
    assert exact(result,furnishing='full')['sample_count']==5
    assert all(row['furnishing'] is None for row in records(rows,rows,furnishing_enabled=True,furnishing_version='other'))


def test_removals_are_observed_durations_and_never_vacancy_or_occupancy():
    live=[source(i,'monthly') for i in range(5)]
    gone=[source(100+i,'monthly',is_active=False,found_at=NOW-timedelta(days=100),
                 first_verified_monthly_at=NOW-timedelta(days=10+i),rental_episode_started_at=NOW-timedelta(days=10+i),
                 last_verified_monthly_at=NOW-timedelta(days=2),rental_episode_last_seen_at=NOW-timedelta(days=2),
                 monthly_proof_days=3,archived_at=NOW-timedelta(days=1),archive_reason='confirmed_gone') for i in range(5)]
    junk=[{**gone[0],'id':'duplicate','is_duplicate':True},
          {**gone[0],'id':'sweep','archive_reason':'sweep_stale'},
          {**gone[0],'id':'short','first_verified_monthly_at':NOW-timedelta(days=2)},
          {**gone[0],'id':'legacy','first_verified_monthly_at':None},
          {**gone[0],'id':'reactivated','rental_episode_started_at':NOW-timedelta(days=1)},
          {**gone[0],'id':'gap','last_verified_monthly_at':NOW-timedelta(days=5)},
          {**gone[0],'id':'daily','rent_period':'daily'}]
    value=exact(records([],live,gone+junk),period='monthly')
    assert value['gone_30d']==5 and value['median_dom_days']==11
    assert value['months_of_supply']==1
    assert not any('vacancy' in key or 'occupancy' in key for key in value)
    assert exact(records([],live,gone[:4]),period='monthly')['median_dom_days'] is None


@pytest.mark.parametrize('changes',[
    {'window_days':180},{'sample_count':4},{'active_supply':0},{'rent_period':'monthly'},
    {'policy_version':'2'},{'source_oldest_at':NOW-timedelta(days=31)},
    {'source_newest_at':NOW-timedelta(hours=73)},{'updated_at':NOW+timedelta(seconds=1)},
    {'p25_price':30000},{'p75_price':15000},
])
def test_reader_fails_closed_on_bad_units_provenance_or_population(changes):
    assert not index.index_ready(aggregate(**changes),NOW,5,'daily',policy=index.DAILY_POLICY)


def test_reader_accepts_90_day_real_history_but_monthly_stays_72h():
    assert index.index_ready(aggregate(window_days=90,source_oldest_at=NOW-timedelta(days=60)),NOW,10,'daily',policy=index.DAILY_POLICY)
    assert not index.index_ready(aggregate('monthly',source_oldest_at=NOW-timedelta(days=4)),NOW,10,'monthly',policy='3')


@pytest.mark.parametrize('text,expected',[
    ('Без мебели','none'),('Мебели нет','none'),('Не меблированная квартира','none'),
    ('Частично меблированная','partial'),('Только кухонный гарнитур','partial'),
    ('Полностью меблирована','full'),('Вся необходимая мебель','full'),
    ('Хороший ремонт','unknown'),('С мебелью','unknown'),('Есть диван','unknown'),
    ('Без мебели, полностью меблирована','unknown'),('Неизвестно','unknown'),
    ('Не полностью меблирована','unknown'),('Не без мебели','unknown'),
])
def test_furnishing_conservative_synthetic_cases(text,expected):
    assert furniture.classify_furnishing('',text)==expected


def test_custom_rules_invalid_regex_cannot_crash_or_enable_indices():
    compiled=furniture.compile_custom_rules('{"partial":["только диван"]}')
    assert furniture.classify_furnishing('','только диван',compiled)=='partial'
    assert furniture.compile_custom_rules('{"full":["(a+)+$"]}')=={}
    assert furniture.compile_custom_rules('broken')=={}
    assert furniture.version_tag('broken').endswith(':invalid')
    assert not furniture.furnishing_validation_ready({'RENTAL_FURNISHING_INDEX_ENABLED':'1'})


def test_manual_audit_template_is_empty_and_synthetic_fifty_cannot_enable():
    assert dataset_template()['records']==[]
    data={'schema_version':1,'dataset_kind':'synthetic','records':[]}
    for i in range(50):
        title,description='Unit case','Полностью меблирована'
        data['records'].append({'listing_id':str(10000+i),'source_url':f'https://krisha.kz/a/show/{10000+i}',
            'checked_at':NOW.isoformat(),'title':title,'description':description,'label':'full','labeler':'unit',
            'label_source':'human_blind','receipt_sha256':hashlib.sha256(str(i).encode()).hexdigest(),
            'text_sha256':hashlib.sha256((title+'\n'+description).encode()).hexdigest()})
    result=evaluate_dataset(data,dataset_sha256='a'*64,now=NOW,receipt_check=lambda row:True)
    assert result['unique_reviewed']==50 and result['accuracy']==1
    assert not result['eligible_for_operator_enable'] and result['status']=='external_validation_pending'


def test_real_gate_requires_explicit_enable_and_reviewed_rules_and_row_version():
    receipt={'schema_version':1,'classifier_version':furniture.VERSION,'dataset_kind':'manual_real_source',
             'unique_reviewed':50,'known_labels_min_support':5,'accuracy':.94,'dataset_sha256':'a'*64,
             'rules_sha256':furniture.rules_sha256(),'reviewed_at':NOW.isoformat(),'eligible_for_operator_enable':True,'errors':[]}
    values={'RENTAL_FURNISHING_INDEX_ENABLED':'1','RENTAL_FURNISHING_RULES_JSON':'{}',
            'RENTAL_FURNISHING_VALIDATION_RECEIPT':json.dumps(receipt)}
    assert furniture.furnishing_index_row_ready({'furnishing_version':furniture.version_tag()},values)
    assert not furniture.furnishing_index_row_ready({'furnishing_version':'old'},values)
    values['RENTAL_FURNISHING_RULES_JSON']='{"full":["ready"]}'
    assert not furniture.furnishing_validation_ready(values)


@pytest.mark.asyncio
async def test_daily_lookup_exact_room_and_scope_fallback_without_city(monkeypatch):
    now=datetime.now(timezone.utc)
    fresh=aggregate(updated_at=now,source_oldest_at=now-timedelta(days=40),source_newest_at=now-timedelta(hours=1),window_days=90)
    fetch=AsyncMock(side_effect=[{**fresh,'sample_count':4},fresh])
    monkeypatch.setattr(pg,'fetchrow',fetch)
    result=await index.lookup_daily_rental_estimate('astana','Есиль','ignored',2,complex_id=10)
    assert result['level']=='район' and result['window_days']==90
    assert fetch.await_count==2 and fetch.await_args.args[4]==2
    assert all('i.rooms=$4' in call.args[0] for call in fetch.await_args_list)
    assert fetch.await_args.args[2]=='Есиль' and fetch.await_args.args[3] is None


@pytest.mark.asyncio
async def test_readers_fail_closed_before_migration_and_for_unvalidated_furniture(monkeypatch):
    fetch=AsyncMock(side_effect=asyncpg.UndefinedTableError('unit missing112'))
    monkeypatch.setattr(pg,'fetchrow',fetch)
    assert await index.lookup_daily_rental_estimate('astana','Есиль',None,2) is None
    assert await index.lookup_hex_rental_estimate('0:0',2) is None
    monkeypatch.setattr(pg,'fetch',AsyncMock(return_value=[]))
    assert await index.lookup_furnished_monthly_estimate('astana','Есиль',None,2) is None


@pytest.mark.asyncio
async def test_catalog_upsert_captures_immutable_history_in_same_statement(monkeypatch):
    value=parser.RentalListing('123456','https://krisha.kz/a/show/123456','2-комнатная',20000,50,2,3,9,
                               'Есиль','Есиль','','astana','apartment','',rent_period='daily')
    monkeypatch.setattr(parser,'resolve_rental_complex',AsyncMock(return_value=None))
    fetch=AsyncMock(return_value={'is_new':True,'reactivated':False})
    monkeypatch.setattr(pg,'fetchrow',fetch)
    assert await parser.save_rental_listings([value])==1
    sql=fetch.await_args.args[0]
    assert 'INSERT INTO rental_observations' in sql and 'FROM observed o' in sql
    assert 'ON CONFLICT(listing_id,observed_on,source,rent_period) DO NOTHING' in sql
    assert 'UPDATE rental_observations' not in sql
    assert 'THEN $15::timestamptz ELSE NULL END' in sql


@pytest.mark.asyncio
async def test_description_reuses_single_budgeted_detail_response(monkeypatch):
    from bot.core import apartment_details,complex_audit
    row={'id':'123456','url':'https://krisha.kz/a/show/123456','title':'2-комнатная','lat':51.128,'lon':71.430,
         'complex_id':10,'complex_name':'Пример','complex_url':'https://krisha.kz/complex/show/10',
         'hex_id':'0:0','hex_edge_m':50.,'description':None,'description_checked_at':None,'furnishing_version':None}
    monkeypatch.setattr(pg,'fetch',AsyncMock(return_value=[row]))
    write=AsyncMock()
    monkeypatch.setattr(pg,'execute',write)
    monkeypatch.setattr(parser,'resolve_rental_complex',AsyncMock(return_value=10))
    monkeypatch.setattr(parser.asyncio,'sleep',AsyncMock())
    monkeypatch.setattr(complex_audit,'street_names',AsyncMock(return_value=set()))
    monkeypatch.setattr(apartment_details,'detail_fetch_cooldown_remaining',lambda:0)
    monkeypatch.setattr(apartment_details,'REQUEST_COUNTS',{'detail':0})
    async def details(*args,**kwargs):
        apartment_details.REQUEST_COUNTS['detail']+=1
        return {'description':'Полностью меблирована','complex_source':'official_map_block',
                'complex_name':'Пример','complex_url':'https://krisha.kz/complex/show/10'}
    fetch=AsyncMock(side_effect=details)
    monkeypatch.setattr(apartment_details,'fetch_apartment_details',fetch)
    result=await parser.backfill_rental_details(1,budget_seconds=30)
    assert fetch.await_count==1 and result['requests']==1 and result['checked']==1
    sql,*params=write.await_args.args
    assert 'description_checked_at=now()' in sql and 'Полностью меблирована' in params and 'full' in params
    assert 'complex_id=COALESCE(complex_id,' in sql
    assert 'last_seen=' not in sql  # enrichment cannot pretend a fresh catalog observation


@pytest.mark.asyncio
async def test_changed_furnishing_rules_reclassify_stored_text_without_http(monkeypatch):
    from bot.core import apartment_details,complex_audit
    row={'id':'123456','url':'https://krisha.kz/a/show/123456','title':'Квартира','lat':51.128,'lon':71.430,
         'complex_id':10,'complex_name':'Пример','complex_url':'https://krisha.kz/complex/show/10',
         'hex_id':'0:0','hex_edge_m':50.,'description':'Полностью меблирована',
         'description_checked_at':NOW,'furnishing_version':'old'}
    monkeypatch.setattr(pg,'fetch',AsyncMock(return_value=[row]))
    write=AsyncMock()
    monkeypatch.setattr(pg,'execute',write)
    monkeypatch.setattr(complex_audit,'street_names',AsyncMock(return_value=set()))
    monkeypatch.setattr(apartment_details,'detail_fetch_cooldown_remaining',lambda:0)
    fetch=AsyncMock(side_effect=AssertionError('No extra HTTP for stored descriptions'))
    monkeypatch.setattr(apartment_details,'fetch_apartment_details',fetch)
    result=await parser.backfill_rental_details(1,budget_seconds=30)
    assert result['requests']==0 and result['checked']==0
    assert write.await_args.args[1:]==('123456','full',furniture.version_tag())
    fetch.assert_not_awaited()


def test_analytics_hides_small_groups_and_empty_hexes_without_guessing_ratio():
    table=web.segment_table([aggregate(),aggregate('monthly'),aggregate(district='Нура',sample_count=9)],NOW)
    assert len(table)==1 and table[0]['price_ratio_pct']==8
    assert not web.hex_feature_collection([aggregate(hex_id='0:0',hex_edge_m=50.,sample_count=4)],NOW,
                                          period='daily',rooms=2,edge_m=50.)['features']
    valid=web.hex_feature_collection([aggregate(hex_id='0:0',hex_edge_m=50.)],NOW,period='daily',rooms=2,edge_m=50.)
    feature=valid['features'][0]
    assert len(feature['geometry']['coordinates'][0])==7
    assert 'listing_id' not in feature['properties'] and feature['properties']['window_days']==30


@pytest.mark.asyncio
async def test_analytics_api_auth_before_database_and_actual_template(monkeypatch):
    app=FastAPI()
    app.include_router(web.make_rental_analytics_router(lambda request:False))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://unit.test') as client:
        response=await client.get('/admin/api/rental-market')
    assert response.status_code==401 and response.headers['Cache-Control']=='no-store'
    context={'segments':web.segment_table([aggregate(),aggregate('monthly')],NOW),'hex_layers':{},'rooms':2,
             'has_monthly':True,'has_daily':True,'has_ratio':True,'has_removal':False,
             'minimum_area':10,'minimum_hex':5,'edge_m':50}
    env=Environment(loader=FileSystemLoader(Path(__file__).resolve().parents[1]/'bot/templates'),autoescape=select_autoescape())
    env.globals['is_admin']=lambda request:True
    request=SimpleNamespace(url=SimpleNamespace(path='/admin/analytics'),cookies={},state=SimpleNamespace(),query_params={})
    html=env.get_template('rental_analytics.html').render(request=request,atab='rental_market',**context)
    assert '20 000 ₸/сут' in html and '250 000 ₸/мес' in html and 'n=10' in html
    assert 'rental-market-map' not in html and 'До снятия с источника' not in html
    assert 'доходность' not in html.lower() or 'Доходность сдачи' not in html


@pytest.mark.asyncio
async def test_public_daily_projection_requires_ready_daily_and_hides_private_fields(monkeypatch):
    rows=[aggregate(listing_id='private-id',contact='private-contact'),aggregate('monthly'),
          aggregate(district='Мало',sample_count=9),aggregate(district='Старое',source_newest_at=NOW-timedelta(hours=73)),
          aggregate(district='Без активных',active_supply=0),aggregate(district='Старый policy',policy_version='old'),
          aggregate('monthly',district='Только месяц'),aggregate(district='Другой город',city='almaty'),
          aggregate(district='Другой ЖК',complex_id=1),aggregate(district='Мебель',furnishing='full')]
    monkeypatch.setattr(pg,'fetch',AsyncMock(return_value=rows))
    context=await web.daily_rental_data(now=NOW)
    assert len(context['segments'])==1 and context['has_monthly'] and context['has_ratio']
    assert context['segments'][0]['price_ratio_pct']==8
    text=json.dumps(context,ensure_ascii=False)
    assert 'private-id' not in text and 'private-contact' not in text and 'hex_id' not in text
    assert 'source_newest_label' in text and 'updated_at_label' in text


@pytest.mark.asyncio
@pytest.mark.parametrize('monthly_ready',[True,False])
async def test_public_daily_route_renders_actual_base_template_without_auth(monkeypatch,monthly_ready):
    now=datetime.now(timezone.utc)
    times={'updated_at':now-timedelta(minutes=1),'source_newest_at':now-timedelta(hours=1),'source_oldest_at':now-timedelta(hours=3)}
    rows=[aggregate(**times),aggregate('monthly',sample_count=10 if monthly_ready else 9,**times)]
    fetch=AsyncMock(return_value=rows)
    monkeypatch.setattr(pg,'fetch',fetch)
    templates=Jinja2Templates(directory=Path(__file__).resolve().parents[1]/'bot/templates')
    templates.env.globals['is_admin']=lambda request:False
    app=FastAPI()
    app.include_router(web.make_rental_analytics_router(lambda request:False,templates))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://unit.test') as client:
        response=await client.get('/rental/daily')
    assert response.status_code==200 and response.headers['Cache-Control']=='no-store'
    assert response.text.count('data-daily-segment')==1 and '20 000' in response.text and '₸/сут' in response.text
    assert 'n=10' in response.text and 'активно 7' in response.text and 'Окно 30 дн.' in response.text
    assert 'Источник:' in response.text and 'Индекс:' in response.text and 'наблюдения' in response.text
    assert ('250 000' in response.text)==monthly_ready
    assert ('<strong>8.0%</strong>' in response.text)==monthly_ready
    assert 'href="/rental/scanner#sublease-form"' in response.text and 'href="/rent"' in response.text
    assert '/admin/api/rental-market' not in response.text and 'rental-market-map' not in response.text
    assert fetch.await_count==1 and 'rental_hex_index' not in fetch.await_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize('migration_missing',[False,True])
async def test_public_daily_empty_or_missing_migration_has_only_intro_and_working_tool(monkeypatch,migration_missing):
    fetch=AsyncMock(side_effect=asyncpg.UndefinedColumnError('112 missing')) if migration_missing else AsyncMock(return_value=[aggregate('monthly')])
    monkeypatch.setattr(pg,'fetch',fetch)
    templates=Jinja2Templates(directory=Path(__file__).resolve().parents[1]/'bot/templates')
    templates.env.globals['is_admin']=lambda request:False
    app=FastAPI()
    app.include_router(web.make_rental_analytics_router(lambda request:False,templates))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://unit.test') as client:
        response=await client.get('/rental/daily')
    assert response.status_code==200 and 'Рассчитать субаренду' in response.text
    assert 'rental-daily-results' not in response.text and '<table' not in response.text
    assert 'Как читать индекс' not in response.text and 'недостаточно данных' not in response.text
    assert '250 000' not in response.text


@pytest.mark.asyncio
async def test_stage2_rebuild_cleanup_is_scoped_and_missing_sources_publish_nothing(monkeypatch):
    connection=SimpleNamespace(execute=AsyncMock(),fetch=AsyncMock(return_value=[]))
    monkeypatch.setattr(index,'_source_rows',AsyncMock(return_value=([],[],[])))
    result=await index.rebuild_rental_stage2_index(connection,NOW)
    sqls=[call.args[0] for call in connection.execute.await_args_list]
    assert result['groups']==0 and result['observation_rows']==0
    assert any("i.rent_period='daily'" in sql and 'i.furnishing IS NOT NULL' in sql for sql in sqls)
    assert not any('UPDATE rental_listings' in sql for sql in sqls)


@pytest.mark.asyncio
async def test_monthly_builder_and_stage2_share_one_rollback_transaction(monkeypatch):
    calls=[]
    class Tx:
        async def __aenter__(self):calls.append('begin')
        async def __aexit__(self,typ,*args):calls.append('rollback' if typ else 'commit')
    connection=SimpleNamespace(transaction=lambda:Tx(),execute=AsyncMock(),fetch=AsyncMock(return_value=[]))
    class Acquire:
        async def __aenter__(self):return connection
        async def __aexit__(self,*args):return False
    monkeypatch.setattr(pg,'get_pool',lambda:SimpleNamespace(acquire=lambda:Acquire()))
    stage=AsyncMock(side_effect=RuntimeError('unit stage2 failure'))
    monkeypatch.setattr(index,'rebuild_rental_stage2_index',stage)
    with pytest.raises(RuntimeError,match='stage2 failure'):
        await parser.rebuild_rental_index()
    assert calls==['begin','rollback'] and stage.await_args.args[0] is connection
