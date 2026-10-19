"""No real DB/source access: sale editorial audit, identity and both medians."""
from datetime import datetime, timezone, timedelta
from copy import deepcopy
from decimal import Decimal
import json

import pytest

from bot.core.sale_editorial_slice import build_sale_editorial_slice, district_key, profiles, KNOWN_SOURCE_CONFLICTS
from tests.test_daily_source_complex_guard import annotated, catalog
from scripts import export_sale_editorial_slice as exporter

NOW = datetime(2026,10,19,10,tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def no_database(monkeypatch):
    monkeypatch.setenv('DATABASE_URL','postgresql://unused:unused@127.0.0.1:1/hatuli_test')
    async def reject(*a,**k):
        raise AssertionError('Production DB forbidden')
    monkeypatch.setattr('asyncpg.connect',reject)
    monkeypatch.setattr('asyncpg.create_pool',reject)
    monkeypatch.setattr('bot.db.pg.init_pool',reject)


def row(i, **changes):
    r=annotated('Возрождение')
    r.update(id=str(1000000000+i),url=f'https://krisha.kz/a/show/{1000000000+i}',
             price=30_000_000+i*100_000,area=60.,rooms=2,address='Туран 6',district='Нура',
             market_type='secondary',last_seen=NOW-timedelta(hours=1),archived_at=None,
             is_active=True,is_duplicate=False,dup_needs_review=False,duplicate_of=None,
             finish_level='renovated',property_id=i+1,property_identity_status='confirmed',
             property_link_count=1,membership_complex_id=3119)
    r['reference_complex_address']='Туран 6'
    r.update(changes)
    return r


def test_joint_profiles_dedup_and_exact_quartiles():
    rows=[row(i) for i in range(12)];rows.append(row(99,property_id=1,last_seen=NOW-timedelta(hours=2)))
    d=build_sale_editorial_slice(rows,now=NOW)
    assert d['counts']['accepted_after_identity_and_dedup']==12
    assert d['counts']['exclusion_reasons_overlapping']['duplicate_confirmed_physical_property']==1
    g=next(g for g in d['ready_profiles'] if g['scope']=='district' and g['area_anchor_m2']==65)
    assert g['median_price']==30_550_000 and g['n_confirmed_physical']==12
    assert g['p25_price']<g['median_price']<g['p75_price']
    assert all(g['n_confirmed_physical']==g['n_observations'] for g in d['ready_profiles'])


@pytest.mark.parametrize('value,expected',[
    ('Есиль',('esil','Есиль')),
    ('Есильский р-н',('esil','Есиль')),
    (' район  ЕСИЛЬСКИЙ ',('esil','Есиль')),
    ('Нура район',('nura','Нура')),
    ('Алматы',('almaty','Алматы')),
    ('Алматинский район',('almaty','Алматы')),
    ('Сарайшык',('saraishyk','Сарайшык')),
    ('р-н Сарайшық',('saraishyk','Сарайшык')),
    ('Сарыарка',('saryarka','Сарыарка')),
    ('Сарыаркинский р-н',('saryarka','Сарыарка')),
    ('Байконур',('baikonur','Байконур')),
    ('Байқоңыр район',('baikonur','Байконур')),
])
def test_explicit_district_aliases_admit_and_label_cohorts(value,expected):
    assert district_key(value)==expected
    d=build_sale_editorial_slice([row(i,district=value) for i in range(12)],now=NOW)
    assert d['counts']['accepted_after_identity_and_dedup']==12
    assert {(g['scope_id'],g['label']) for g in d['ready_profiles'] if g['scope']=='district'}=={expected}


@pytest.mark.parametrize('value',[
    'Есиль, Кабанбай батыра 19',
    'Есильский р-н, проспект Кабанбай батыра 19',
    'Есиль — лучшая квартира в центре',
    'рядом с ТД Сарыарка',
    'Сарыарка 5',
    'Сарайшык 7',
    'в районе Есиль',
    'Нура / Есиль',
    'Нурарайон',
    '',None,123,
])
def test_embedded_district_text_is_unknown_and_cannot_change_either_median(value):
    assert district_key(value) is None
    good=[row(i) for i in range(12)]
    d=build_sale_editorial_slice(good+[row(99,district=value,price=90_000_000)],now=NOW)
    expected=build_sale_editorial_slice(good,now=NOW)
    assert d['counts']['accepted_after_identity_and_dedup']==12
    assert d['decisions'][-1]['reasons']==['district_unknown']
    assert d['ready_profiles']==expected['ready_profiles']
    assert d['descriptive_cohorts']==expected['descriptive_cohorts']
    # The pure profile helper must also fail closed if called separately.
    unsafe=dict(expected['accepted_rows'][0],district=value)
    assert profiles([unsafe]*12)==[]


def test_known_akerke_conflict_removed_even_if_database_name_was_overwritten():
    good=[row(i) for i in range(12)];bad=row(88,id='1012143050',url='https://krisha.kz/a/show/1012143050',price=90_000_000)
    # This row passes the stored-name gate because it already says Возрождение.
    actual=build_sale_editorial_slice(good+[bad],now=NOW)
    expected=build_sale_editorial_slice(good,now=NOW)
    assert actual['ready_profiles']==expected['ready_profiles']
    assert actual['descriptive_cohorts']==expected['descriptive_cohorts']
    assert 'known_source_conflict' in next(d for d in actual['decisions'] if d['id']==bad['id'])['reasons']
    assert KNOWN_SOURCE_CONFLICTS[0]['observed_date']=='2026-10-19'
    assert 'observed_at' not in KNOWN_SOURCE_CONFLICTS[0]


def test_other_publication_of_held_property_cannot_influence_either_median():
    bad=row(88,id='1012143050',url='https://krisha.kz/a/show/1012143050')
    relist=row(89,property_id=bad['property_id'])
    d=build_sale_editorial_slice([row(i) for i in range(12)]+[bad,relist],now=NOW)
    assert d['counts']['accepted_after_identity_and_dedup']==12
    assert 'known_source_conflict_property' in next(x for x in d['decisions'] if x['id']==relist['id'])['reasons']


def test_conflict_hold_survives_original_source_leaving_active_slice():
    d=build_sale_editorial_slice([row(i) for i in range(12)]+[row(99,property_id=999)],
                                now=NOW,held_property_ids=[999])
    assert d['counts']['accepted_after_identity_and_dedup']==12
    assert 'known_source_conflict_property' in d['decisions'][-1]['reasons']


def test_confirmed_status_without_physical_key_does_not_make_ready_profile():
    d=build_sale_editorial_slice([row(i,property_id=None) for i in range(12)],now=NOW)
    assert not d['ready_profiles']
    assert all(g['n_confirmed_physical']==0 for g in d['descriptive_cohorts'])


def test_stale_browser_receipts_cannot_relabel_unvisited_market():
    with pytest.raises(ValueError,match='fresh'):
        build_sale_editorial_slice([row(1)],now=NOW,browser_checks={'x':{'checked_at':NOW-timedelta(hours=3)}})


def test_umbrella_without_exact_house_is_not_a_comparable():
    d=build_sale_editorial_slice([row(1,identity_complex_is_umbrella=True)],now=NOW)
    assert 'umbrella_house_unresolved' in d['decisions'][0]['reasons']


@pytest.mark.parametrize('value',[Decimal('NaN'),Decimal('sNaN'),Decimal('Infinity'),Decimal('1e9999'),True])
def test_nonfinite_numeric_and_bool_cannot_enter_price_distribution(value):
    d=build_sale_editorial_slice([row(1,price=value)],now=NOW)
    assert not d['accepted_rows']


def test_postgresql_numeric_values_are_normalized_without_losing_sample():
    d=build_sale_editorial_slice([row(i,price=Decimal(30_000_000+i*100_000),area=Decimal('60.0')) for i in range(12)],now=NOW)
    assert d['counts']['accepted_after_identity_and_dedup']==12 and d['ready_profiles']


def test_multi_property_relation_is_not_min_id_as_confirmed_identity():
    d=build_sale_editorial_slice([row(1,property_link_count=2)],now=NOW)
    assert not d['accepted_rows'] and 'property_review_pending' in d['decisions'][0]['reasons']
    assert 'count(DISTINCT property_id)' in exporter.SALES_SQL


@pytest.mark.parametrize('source,reference,expected',[
    ('Туран 6/1','Туран 6/2','house_number_conflict'),
    ('Туран 6б','Туран 6','house_number_conflict'),
    ('Туран 6','Косшыгулулы 6','street_spelling_unresolved'),
    ('Туран 6',None,'catalog_reference_unavailable'),
])
def test_reference_addresses_preserve_fraction_letters_and_unknown(source,reference,expected):
    rows=[row(i,address=source,reference_complex_address=reference) for i in range(12)]
    d=build_sale_editorial_slice(rows,now=NOW)
    if expected=='house_number_conflict':
        assert not d['accepted_rows'] and 'catalog_house_number_conflict' in d['decisions'][0]['reasons']
    else:
        assert not d['ready_profiles'] and d['descriptive_cohorts']
        assert all(g['scope']=='district' for g in d['descriptive_cohorts'])
        assert all(r['address_reference_status']==expected for r in d['accepted_rows'])


@pytest.mark.parametrize('change,reason',[
    ({'identity_name_ambiguous':True},'ambiguous_complex_name'),
    ({'identity_source_name_conflict':True},'stored_source_name_conflict'),
    ({'identity_property_match_pending':True},'property_review_pending'),
    ({'identity_complex_review_pending':True},'complex_review_pending'),
    ({'address':None},'address_missing_or_ambiguous'),
    ({'is_duplicate':True},'known_duplicate'),
    ({'price':101_000_000},'outside_price_budget'),
    ({'last_seen':NOW-timedelta(hours=73)},'stale_or_future'),
    ({'market_type':'primary'},'not_secondary'),
])
def test_exclusion_gates(change,reason):
    d=build_sale_editorial_slice([row(1,**change)],now=NOW)
    assert d['counts']['accepted_after_identity_and_dedup']==0
    assert reason in d['decisions'][0]['reasons']


def test_unknown_condition_and_provisional_are_not_ready_but_not_falsely_duplicates():
    rows=[row(i,property_identity_status='provisional') for i in range(12)]
    d=build_sale_editorial_slice(rows,now=NOW)
    assert not d['ready_profiles'] and d['descriptive_cohorts']
    assert all(g['n_confirmed_physical']==0 and not g['ready'] for g in d['descriptive_cohorts'])
    d=build_sale_editorial_slice([row(i,finish_level=None) for i in range(12)],now=NOW)
    assert not d['ready_profiles'] and d['counts']['quality_diagnostics_overlapping']['condition_missing']==12


def test_unchanged_inputs_and_unvisited_not_browser_verified():
    rows=[row(i) for i in range(12)];before=deepcopy(rows)
    d=build_sale_editorial_slice(rows,now=NOW)
    assert rows==before
    assert all(r['verification']=='database_validated_only' for r in d['decisions'])


def test_room_condition_and_area_strata_do_not_mix():
    rows=[row(i) for i in range(9)]+[row(30,rooms=3),row(31,area=150),row(32,finish_level='rough')]
    assert not build_sale_editorial_slice(rows,now=NOW)['ready_profiles']


def test_no_truncated_population_or_duplicate_join_rows(monkeypatch):
    monkeypatch.setattr('bot.core.sale_editorial_slice.MAX_ROWS',1)
    with pytest.raises(ValueError,match='Incomplete'):build_sale_editorial_slice([row(1),row(2)],now=NOW)
    monkeypatch.setattr('bot.core.sale_editorial_slice.MAX_ROWS',100)
    with pytest.raises(ValueError,match='Repeated'):build_sale_editorial_slice([row(1),row(1)],now=NOW)


def test_private_write_is_immutable_and_has_no_secret_fields(tmp_path):
    path=tmp_path/'audit.json';d=build_sale_editorial_slice([row(1)],now=NOW)
    out=exporter.write_private(path,d)
    assert len(out['sha256'])==64 and json.loads(path.read_text(encoding='utf-8'))['counts']['input_rows']==1
    with pytest.raises(ValueError,match='immutable'):exporter.write_private(path,d)


@pytest.mark.asyncio
async def test_reader_fails_closed_without_readonly_transaction():
    class Transaction:
        async def __aenter__(self):return self
        async def __aexit__(self,*a):pass
    class Connection:
        closed=False
        def transaction(self,**kw):
            assert kw==dict(readonly=True,isolation='repeatable_read');return Transaction()
        async def fetchval(self,q):return 'off'
        async def fetch(self,*a):raise AssertionError('Must reject before reading')
        async def close(self):self.closed=True
    c=Connection()
    async def connect(dsn,**kw):
        assert kw['server_settings']['default_transaction_read_only']=='on';return c
    with pytest.raises(ValueError,match='Read-only'):await exporter.read_sale_slice('placeholder',NOW,connect=connect)
    assert c.closed


@pytest.mark.asyncio
async def test_reader_uses_one_repeatable_read_and_bounded_existing_identity_evidence():
    class Transaction:
        async def __aenter__(self):return self
        async def __aexit__(self,*a):pass
    class Connection:
        closed=False
        reads=[]
        def transaction(self,**kw):
            assert kw==dict(readonly=True,isolation='repeatable_read');return Transaction()
        async def fetchval(self,q):
            return 'on' if q=='SHOW transaction_read_only' else 'repeatable read'
        async def fetch(self,q,*params):
            self.reads.append(q)
            assert q.lstrip().upper().startswith(('SELECT','WITH'))
            if q==exporter.SALES_SQL:return [row(1)]
            if q==exporter.MEMBERSHIP_SQL:return [dict(listing_id=row(1)['id'],complex_id=3119)]
            if q.lstrip().startswith('SELECT id,name,address,'):
                return [dict(catalog(),address='Туран 6')]
            return []
        async def close(self):self.closed=True
    c=Connection()
    async def connect(dsn,**kw):
        assert kw['server_settings']['default_transaction_read_only']=='on';return c
    out=await exporter.read_sale_slice('placeholder',NOW,connect=connect)
    assert c.closed and out['database_writes']==0
    assert out['rows'][0]['reference_complex_address']=='Туран 6'
    assert out['rows'][0]['identity_checked'] is True
    assert out['rows'][0]['membership_complex_id']==3119
    assert len(c.reads)==8
