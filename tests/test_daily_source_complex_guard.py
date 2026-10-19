"""Source/canonical ЖК conflicts must leave both ranking and price peers."""
import copy
from types import SimpleNamespace

import pytest

from bot.core.complex_ingest_identity import normalize_complex_name as key
from bot.core.daily_pick_identity import (annotate_daily_pick_identity_rows,
    normalize_daily_pick_identity, matching_source_complex_name)
from scripts import publish_daily_picks as publisher
from scripts import publish_daily_picks_browser as browser
from tests.test_daily_picks import row, rows, NOW
from tests.test_daily_picks_browser import item, observation


@pytest.fixture(autouse=True)
def no_io(monkeypatch):
    async def reject(*args, **kwargs):
        raise AssertionError('Source complex guard tests cannot access a database')
    monkeypatch.setattr('asyncpg.connect', reject)
    monkeypatch.setattr('asyncpg.create_pool', reject)


@pytest.mark.asyncio
async def test_public_generator_requires_explicit_browser_evidence_before_database_read(monkeypatch):
    monkeypatch.setenv('DATABASE_URL', 'postgresql://unused:unused@127.0.0.1:1/hatuli_test_guard')
    monkeypatch.setattr('dotenv.load_dotenv', lambda *args: False)
    with pytest.raises(ValueError, match='normal-browser'):
        await publisher.generate(SimpleNamespace(skip_photo_check=False, source_checks=None), NOW)


def catalog(cid=3119, name='Возрождение', **changes):
    return dict(id=cid, name=name, name_key=key(name), canonical_id=None,
                is_garbage=False, is_street=False, canonical_reason=None,
                is_umbrella=False, parent_complex_id=None, year_built=2020, **changes)


def annotated(name='Акерке 2', *, complexes=None, aliases=None, **changes):
    value=row(0, complex_id=3119, complex_resolution='name', address='Косшыгулулы, 6',
              identity_raw_name_key=key(name), **changes)
    return annotate_daily_pick_identity_rows([value], complexes=complexes or [catalog()],
        aliases=aliases or [], complex_reviews=[], relations=[], property_reviews=[])[0]


def test_same_address_never_makes_akerke_and_vozrozhdenie_equivalent():
    value=annotated()
    assert value['identity_source_name_conflict'] is True
    assert normalize_daily_pick_identity(value) is None
    assert not matching_source_complex_name(value, {'source_complex_name':'Акерке 2'})


def test_unreviewed_canonical_redirect_cannot_approve_a_source_spelling():
    redirect=catalog(44, 'Акерке 2');redirect['canonical_id']=3119
    assert normalize_daily_pick_identity(annotated(complexes=[catalog(),redirect])) is None


@pytest.mark.parametrize('status', ['observed','rejected'])
def test_unverified_alias_is_not_a_source_name_proof(status):
    alias=dict(complex_id=3119, normalized_name=key('Акерке 2'), status=status)
    assert normalize_daily_pick_identity(annotated(aliases=[alias])) is None


def test_reviewed_unambiguous_alias_is_accepted_without_rebinding():
    value=annotated(aliases=[dict(complex_id=3119,normalized_name=key('Акерке 2'),status='verified')])
    before=copy.deepcopy(value)
    assert normalize_daily_pick_identity(value)['complex_id']==3119
    assert matching_source_complex_name(value, {'source_complex_name':'ЖК «Акерке 2»'})
    assert value==before


def test_ambiguous_verified_name_does_not_pick_one_catalog_owner():
    aliases=[dict(complex_id=cid,normalized_name=key('Акерке 2'),status='verified') for cid in (3119,3120)]
    value=annotated(complexes=[catalog(),catalog(3120,'Другой ЖК')],aliases=aliases)
    assert normalize_daily_pick_identity(value) is None


@pytest.mark.parametrize('observed', [None,'','Возрождение 2','Возрождение с паркингом',17,True])
def test_missing_or_prefix_similar_name_does_not_confirm_a_bound_complex(observed):
    value=annotated('Возрождение')
    assert not matching_source_complex_name(value, {'source_complex_name':observed})
    assert not matching_source_complex_name(value, {})


def test_exact_house_under_umbrella_still_requires_independent_house_evidence():
    parent=catalog(1,'Проект');house=catalog(2,'Проект корпус 2');house['parent_complex_id']=1
    value=row(0,complex_id=1,complex_resolution='url',resolved_house_id=2,
              house_attribution='address',identity_raw_name_key=key('Проект'))
    value=annotate_daily_pick_identity_rows([value],complexes=[parent,house],aliases=[],
        complex_reviews=[],relations=[],property_reviews=[])[0]
    assert normalize_daily_pick_identity(value)['resolved_house_id']==2
    assert matching_source_complex_name(value, {'source_complex_name':'Проект'})
    assert matching_source_complex_name(value, {'source_complex_name':'Проект корпус 2'})
    value['house_attribution']='geo'
    assert normalize_daily_pick_identity(value) is None


def check(value, **changes):
    return dict(id=value['id'],source_url=value['url'],status='active',sale_astana=True,
        price=value['price'],rooms=value['rooms'],area=value['area'],floor=value['floor'],
        floors_total=value['floors_total'],source_complex_name='Тестовый ЖК',**changes)


def test_fresh_conflict_is_removed_before_peer_count_and_price_comparison():
    baseline=rows()
    for value in baseline:
        value['identity_source_name_keys']=[key('Тестовый ЖК')]
    bad=row(99,price=70_000_000,identity_source_name_keys=[key('Тестовый ЖК')])
    record=check(bad);record['source_complex_name']='Другой ЖК'
    filtered=publisher.source_identity_rows(baseline+[bad],{bad['id']:record})
    assert bad not in filtered
    actual=publisher.build_daily_picks(filtered,NOW)
    expected=publisher.build_daily_picks(baseline,NOW)
    assert [v['price_comparison'] for v in actual['items']]==[v['price_comparison'] for v in expected['items']]


@pytest.mark.parametrize('change',[{'rooms':3},{'floor':4},{'status':'archived'}, {'source_complex_name':None}])
def test_changed_source_facts_are_removed_before_ranking(change):
    value=row(0,identity_source_name_keys=[key('Тестовый ЖК')]);record=check(value);record.update(change)
    assert publisher.source_identity_rows([value],{value['id']:record})==[]


def test_old_receipt_without_name_fails_closed_but_unvisited_peers_are_not_claimed_fresh():
    value=row(0,identity_source_name_keys=[key('Тестовый ЖК')]);record=check(value);record.pop('source_complex_name')
    assert publisher.source_identity_rows([value],{value['id']:record})==[]
    assert publisher.source_identity_rows([value],{})==[value]


def test_source_address_only_requires_explicit_absence_and_never_invents_a_complex():
    value=dict(complex_id=None,resolved_house_id=None)
    assert matching_source_complex_name(value,{'source_complex_name':None})
    assert not matching_source_complex_name(value,{})
    assert not matching_source_complex_name(value,{'source_complex_name':'Новый ЖК'})


@pytest.mark.parametrize('name', [None,'Акерке 2'])
def test_browser_receipt_carries_the_explicit_structured_source_observation(name):
    record=browser.source_record(item(),observation(source_complex_name=name),NOW)
    assert record['status']=='active' and record['source_complex_name']==name


def test_browser_missing_or_conflicting_structured_observation_remains_unknown():
    missing=observation();missing.pop('source_complex_name')
    assert browser.source_record(item(),missing,NOW)['status']=='unknown'
    assert browser.source_record(item(),observation(source_complex_name='Акерке 2',
        source_complex_name_conflict=True),NOW)['status']=='unknown'
