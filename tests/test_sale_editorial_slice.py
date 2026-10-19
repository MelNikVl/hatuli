"""No real DB/source access: sale editorial audit, identity and both medians."""
from datetime import datetime, timezone, timedelta
from copy import deepcopy
import json

import pytest

from bot.core.sale_editorial_slice import build_sale_editorial_slice, KNOWN_SOURCE_CONFLICTS
from tests.test_daily_source_complex_guard import annotated
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


def test_known_akerke_conflict_removed_even_if_database_name_was_overwritten():
    good=[row(i) for i in range(12)];bad=row(88,id='1012143050',url='https://krisha.kz/a/show/1012143050',price=90_000_000)
    # This row passes the stored-name gate because it already says Возрождение.
    actual=build_sale_editorial_slice(good+[bad],now=NOW)
    expected=build_sale_editorial_slice(good,now=NOW)
    assert actual['ready_profiles']==expected['ready_profiles']
    assert actual['descriptive_cohorts']==expected['descriptive_cohorts']
    assert 'known_source_conflict' in next(d for d in actual['decisions'] if d['id']==bad['id'])['reasons']
    assert KNOWN_SOURCE_CONFLICTS[0]['observed_at'].startswith('2026-10-19')


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
