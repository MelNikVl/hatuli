"""Monthly rental selection and publication use genuine, independently checked facts."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import sys
from types import SimpleNamespace

import pytest

from bot.core import rental_picks as picks
from scripts import publish_rental_picks as publisher

NOW=datetime(2026,10,16,6,tzinfo=timezone.utc)
PHOTO='https://krisha-photos.kcdn.online/webp/10/01234567-89ab-cdef-0123-456789abcdef/1-full.webp'


def row(index=1,**changes):
    identifier=str(1000000000+index)
    return {'id':identifier,'url':f'https://krisha.kz/a/show/{identifier}',
            'title':'2-комнатная квартира, помесячно','city':'astana','prop_type':'apartment',
            'rent_period':'monthly','rent_period_conflict':False,'price':250000,'area':55.0,'rooms':2,
            'floor':8,'floors_total':11,'district':'Есильский','address':'Туран, 12',
            'is_active':True,'archived_at':None,'is_duplicate':False,'dup_needs_review':False,
            'duplicate_of':None,'last_seen':NOW,'photo_url':PHOTO,'photos':[PHOTO],
            'complex_id':None,'index_complex_id':None,'index_complex_name':None,
            'complex_binding_verified':False,'hex_id':None,'hex_edge_m':None,
            'rental_episode_started_at':NOW-timedelta(days=2),
            'rental_episode_last_seen_at':NOW,'observations':[],**changes}


def checked(value,**changes):
    return {'id':value['id'],'source_url':value['url'],'status':'active','rent_astana':True,
            'rent_period':'monthly','checked_at':NOW.isoformat(),
            **{key:value[key] for key in ('price','rooms','area','floor','floors_total')},
            'facts':{'is_owner':True,'furniture_present':True},**changes}


def population(value=None,count=10,**peer_changes):
    value=value or row()
    peers=[row(100+i,price=300000,**peer_changes) for i in range(count)]
    return value,peers


def select(value=None,peers=None,check=None):
    value=value or row()
    if peers is None:
        _,peers=population(value)
    receipt=checked(value) if check is None else check
    return picks.build_rental_picks([value,*peers],now=NOW,checks={value['id']:receipt})


def full_edition():
    values=[row(i+1) for i in range(10)]
    peers=[row(100+i,price=300000) for i in range(11)]
    return picks.build_rental_picks([*values,*peers],now=NOW,
        checks={value['id']:checked(value) for value in values})


@pytest.fixture(autouse=True)
def no_database(monkeypatch):
    monkeypatch.setenv('DATABASE_URL','postgresql://unused:unused@127.0.0.1:1/hatuli_rental_picks_test')
    async def reject(*args,**kwargs):
        raise AssertionError('Rental selection unit tests must not connect to PostgreSQL')
    monkeypatch.setattr('asyncpg.connect',reject)
    monkeypatch.setattr('asyncpg.create_pool',reject)


def test_ready_offer_uses_exact_monthly_rooms_area_local_peers_and_four_facts():
    result=select()
    assert len(result['items'])==1
    item=result['items'][0]
    assert item['rent_period']=='monthly' and item['price']==250000
    assert item['price_comparison']['discount_pct']==16.7
    assert item['price_comparison']['comparable_count']==10
    assert item['price_comparison']['scope']=='в этом районе'
    assert len(item['reasons'])==4
    assert any('16.7%' in reason for reason in item['reasons'])
    assert any('хозяин' in reason for reason in item['reasons'])
    assert any('указана мебель' in reason for reason in item['reasons'])
    assert all('полностью меблирована' not in reason for reason in item['reasons'])
    assert '8 этаж из 11.' in item['reasons']
    assert all('между первым' not in reason for reason in item['reasons'])


@pytest.mark.parametrize('changes',[
    {'rent_period':'daily'},{'rent_period':'unknown'},{'rent_period_conflict':True},
    {'title':'Квартира посуточно'},{'title':'Квартира на ночь, почасовая аренда'},
    {'is_duplicate':True},{'dup_needs_review':True},{'duplicate_of':'other'},
    {'is_active':False},{'archived_at':NOW},{'city':'almaty'},{'prop_type':'parking'},
    {'last_seen':NOW-timedelta(hours=73)},{'last_seen':NOW+timedelta(seconds=1)},
    {'complex_id':17,'complex_binding_verified':False},
    {'floor':0},{'floor':12},{'floors_total':0},{'area':float('nan')},
])
def test_ineligible_or_conflicting_source_offer_is_never_selected(changes):
    assert select(row(**changes))['items']==[]


@pytest.mark.parametrize('changes',[
    {'id':'1000000002'},{'source_url':'https://krisha.kz/a/show/1000000002'},
    {'status':'archived'},{'status':'deleted'},{'status':'unknown'},
    {'rent_astana':False},{'rent_period':'daily'},{'checked_at':(NOW-timedelta(hours=2,seconds=1)).isoformat()},
    {'checked_at':(NOW+timedelta(seconds=1)).isoformat()},
    {'price':250001},{'rooms':3},{'floor':7},{'floors_total':12},{'area':55.051},
    {'area':True},{'price':True},{'area':float('inf')},
])
def test_browser_confirmation_must_match_identity_price_shape_and_freshness(changes):
    value=row()
    assert select(value,check=checked(value,**changes))['items']==[]


def test_float4_area_rounding_tolerance_does_not_make_a_genuine_source_mismatch():
    value=row(area=68.19999694824219)
    _,peers=population(value,area=68.2)
    result=select(value,peers,checked(value,area=68.2))
    assert len(result['items'])==1
    assert result['items'][0]['area']==68.2


@pytest.mark.parametrize('changes',[
    {'rooms':3},{'area':46.7},{'area':63.3},{'district':'Сарыаркинский'},
    {'is_duplicate':True},{'dup_needs_review':True},{'rent_period':'daily'},
    {'title':'Аренда посуточно'},
])
def test_incompatible_or_duplicate_peers_cannot_supply_missing_sample(changes):
    value,peers=population(count=9)
    bad=row(150,price=300000,**changes)
    assert select(value,[*peers,bad])['items']==[]


def test_candidate_itself_and_repeated_ids_do_not_count_as_analogues():
    value,peers=population(count=9)
    assert select(value,[*peers,deepcopy(value)])['items']==[]
    assert select(value,[*peers,deepcopy(peers[0])])['items']==[]


def test_verified_complex_requires_five_peers_and_does_not_inherit_umbrella_name():
    value=row(complex_id=17,index_complex_id=17,index_complex_name='Проверенный дом',complex_binding_verified=True)
    _,peers=population(value,count=5,complex_id=17,index_complex_id=17,
                       index_complex_name='Проверенный дом',complex_binding_verified=True)
    result=select(value,peers)
    assert result['items'][0]['price_comparison']['scope']=='в этом ЖК'
    assert result['items'][0]['price_comparison']['comparable_count']==5
    assert result['items'][0]['complex_name']=='Проверенный дом'
    assert select(value,peers[:4])['items']==[]


def test_stable_legacy_monthly_sources_can_compare_without_inventing_episode_history():
    value=row(rental_episode_started_at=None,rental_episode_last_seen_at=None,
              rental_price_history=[{'old_price':9000000,'new_price':250000,'changed_at':NOW.isoformat()}])
    _,peers=population(value,rental_episode_started_at=None,rental_episode_last_seen_at=None)
    result=select(value,peers)
    assert len(result['items'])==1
    assert result['items'][0]['price_history'] is None
    assert result['items'][0]['rental_episode_started_at'] is None


@pytest.mark.parametrize('price,expected',[(285000,True),(288000,False),(210000,True),(207000,False)])
def test_price_discount_must_be_between_five_and_thirty_percent(price,expected):
    assert bool(select(row(price=price))['items']) is expected


def test_budget_applies_to_selected_offer_not_reference_population():
    value=row(price=500000)
    _,peers=population(value)
    peers=[{**peer,'price':600000} for peer in peers]
    assert len(select(value,peers)['items'])==1
    assert select(row(price=500001),peers)['items']==[]


def test_wide_inconsistent_peer_price_distribution_is_rejected():
    value,peers=population()
    peers=[{**peer,'price':100000 if index<5 else 600000} for index,peer in enumerate(peers)]
    assert select(value,peers)['items']==[]


def test_checked_receipt_must_support_four_different_factors():
    value=row()
    assert select(value,check=checked(value,facts={}))['items']==[]
    # Unknown or explicitly false furnishing is not published as an advantage.
    assert select(value,check=checked(value,facts={'is_owner':True,'furniture_present':False}))['items']==[]
    private=picks.build_rental_picks([value,*population()[1]],now=NOW,require_checks=False)
    assert len(private['items'])==1 and len(private['items'][0]['reasons'])<4


def test_source_furniture_and_two_explicit_appliances_do_not_claim_complete_furnishing():
    value=row()
    result=select(value,check=checked(value,facts={'is_owner':False,'furniture_present':True,'washer_and_fridge':True}))
    reasons=result['items'][0]['reasons']
    assert len(reasons)==4
    assert any('Холодильник и стиральная машина' in reason for reason in reasons)
    assert not any('хозяин' in reason or 'полностью' in reason for reason in reasons)
    assert result['items'][0]['selection_facts']=={'furniture_present':True,'washer_and_fridge':True}


def test_contact_flat_and_pasted_description_are_not_public_address_or_payload():
    value=row(address='Туран, 12, кв. 44 — PRIVATE-PHONE +77011234567',description='PRIVATE-DESCRIPTION',
              seller_phone='PRIVATE-PHONE',photos=[PHOTO,'https://evil.example/PRIVATE-PHOTO'])
    item=select(value)['items'][0]
    assert item['address']=='Туран, 12'
    assert item['photo_urls']==[PHOTO]
    assert 'PRIVATE' not in json.dumps(item)
    assert set(item)<=set(picks.FIELDS)
    assert select(row(photo_url='https://evil.example/image.jpg',photos=[]))['items']==[]


def observation(at,price,**changes):
    return {'observed_at':at.isoformat(),'price':price,'rent_period':'monthly','period_source_checked':True,
            'episode_started_at':(NOW-timedelta(days=2)).isoformat(),**changes}


def test_real_same_episode_price_change_has_graph_event_and_positive_cut_reason():
    value=row(area=55.0,observations=[observation(NOW-timedelta(days=1),280000),observation(NOW,250000)])
    receipt=checked(value,facts={'is_owner':True})
    result=select(value,check=receipt)
    assert len(result['items'])==1
    history=result['items'][0]['price_history']
    assert history['status']=='consistent' and history['changes_count']==1
    assert history['events'][0]['at']==NOW.isoformat()
    assert history['net_change']==-30000
    assert any('снизили на 30 000' in reason for reason in result['items'][0]['reasons'])


@pytest.mark.parametrize('changes',[
    {'rent_period':'daily'},{'period_source_checked':False},
    {'episode_started_at':(NOW-timedelta(days=30)).isoformat()},
    {'observed_at':(NOW+timedelta(days=1)).isoformat()},
])
def test_foreign_period_episode_or_future_point_cannot_create_a_price_history(changes):
    value=row(observations=[observation(NOW-timedelta(days=1),280000,**changes)])
    history=select(value)['items'][0]['price_history']
    assert history['events']==[] and history['net_change'] is None


def test_minimum_edition_and_failed_atomic_publication_preserve_last_good_file(tmp_path,monkeypatch):
    edition=full_edition()
    assert len(edition['items'])==10
    path=tmp_path/'rental-picks.json'
    picks.publish_snapshot(edition,path)
    before=path.read_bytes()
    incomplete={**edition,'items':edition['items'][:9]}
    with pytest.raises(ValueError,match='ten'):
        picks.publish_snapshot(incomplete,path)
    assert path.read_bytes()==before
    invalid=deepcopy(edition)
    invalid['items'][0]['seller_phone']='PRIVATE-PHONE'
    with pytest.raises(ValueError):
        picks.publish_snapshot(invalid,path)
    assert path.read_bytes()==before
    def reject(*args):
        raise OSError('simulated atomic replace failure')
    monkeypatch.setattr(picks.os,'replace',reject)
    with pytest.raises(OSError):
        picks.publish_snapshot(edition,path)
    assert path.read_bytes()==before
    assert list(tmp_path.iterdir())==[path]


def test_snapshot_does_not_trust_preformatted_reasons_or_methodology():
    edition=full_edition()
    edition['methodology']={'summary':'UNTRUSTED-COPY'}
    edition['items'][0]['reasons']=['UNTRUSTED-COPY']*4
    safe=picks.validate_snapshot(edition)
    assert 'UNTRUSTED-COPY' not in json.dumps(safe)
    assert len(safe['items'][0]['reasons'])==4


def test_prepare_passes_personal_episode_identity_to_offline_forecast(monkeypatch):
    edition=full_edition()
    seen=[]
    def model(value,*,now):
        seen.append(value)
        return {'removal':{'median_days':12,'low_days':4,'high_days':20,'as_of_label':'16.10.2026'}}
    monkeypatch.setattr('bot.core.rental_forecast_runtime.rental_forecast_for_offer',model)
    result=picks.prepare_rental_picks(edition,now=NOW)
    assert len(seen)==10
    assert seen[0]['city']=='astana' and seen[0]['is_active'] is True
    assert seen[0]['last_seen']==edition['items'][0]['observed_at']
    assert seen[0]['rent_period']=='monthly'
    assert seen[0]['rental_episode_started_at']==edition['items'][0]['rental_episode_started_at']
    assert 'removal' in result['items'][0]['rental_forecast']
    assert picks.prepare_rental_picks(edition,now=NOW+timedelta(hours=25)) is None


class Transaction:
    async def __aenter__(self): pass
    async def __aexit__(self,*args): pass


class Connection:
    def __init__(self,readonly='on',rows=None):
        self.readonly=readonly
        self.rows=rows or []
        self.closed=False
        self.query=None
    def transaction(self,**options):
        self.options=options
        return Transaction()
    async def fetchval(self,sql):
        assert sql=='SHOW transaction_read_only'
        return self.readonly
    async def fetch(self,sql,now):
        self.query=sql
        assert now==NOW
        return self.rows
    async def close(self): self.closed=True


@pytest.mark.asyncio
@pytest.mark.parametrize('readonly',['on','off'])
async def test_export_uses_readonly_raw_connection_without_pool_or_migrations(monkeypatch,readonly):
    connection=Connection(readonly,[row()])
    options={}
    async def connect(dsn,**kwargs):
        assert dsn=='postgresql://unused:unused@127.0.0.1:1/placeholder'
        options.update(kwargs)
        return connection
    monkeypatch.setitem(sys.modules,'asyncpg',SimpleNamespace(connect=connect))
    if readonly=='off':
        with pytest.raises(RuntimeError,match='Read-only'):
            await publisher.read_candidates('postgresql://unused:unused@127.0.0.1:1/placeholder',NOW)
        assert connection.query is None
    else:
        result=await publisher.read_candidates('postgresql://unused:unused@127.0.0.1:1/placeholder',NOW)
        assert len(result)==1
        assert connection.query.lstrip().startswith('WITH')
        assert 'r.title' in connection.query and "o.rent_period='monthly'" in connection.query
        assert 'o.episode_started_at=r.rental_episode_started_at' in connection.query
    assert options['server_settings']['default_transaction_read_only']=='on'
    assert connection.options=={'readonly':True,'isolation':'repeatable_read'}
    assert connection.closed


def test_browser_check_file_requires_explicit_method_and_unique_ids(tmp_path):
    path=tmp_path/'checks.json'
    path.write_text(json.dumps({'method':'normal_browser','items':[checked(row())]}),encoding='utf-8')
    assert publisher.source_checks(path)[row()['id']]['status']=='active'
    path.write_text(json.dumps({'method':'http_fetch','items':[]}),encoding='utf-8')
    with pytest.raises(ValueError,match='Ordinary browser'):
        publisher.source_checks(path)
    path.write_text(json.dumps({'method':'normal_browser','items':[checked(row()),checked(row())]}),encoding='utf-8')
    with pytest.raises(ValueError,match='Duplicate'):
        publisher.source_checks(path)
