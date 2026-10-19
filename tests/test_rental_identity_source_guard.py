"""Source observations remain distinct from canonical FK decisions; mock only."""
from unittest.mock import AsyncMock

import pytest

from bot.core import complex_ingest_identity as shared
from bot.core import house_resolution as houses
from bot.core import rental_stage0_identity as identity
from bot.db import pg

URL='https://krisha.kz/complex/show/10'


@pytest.fixture(autouse=True)
def no_database(monkeypatch):
    monkeypatch.setenv('DATABASE_URL','postgresql://unused:unused@127.0.0.1:1/rental_identity_guard')
    async def reject(*args,**kwargs):
        raise AssertionError('No real PostgreSQL or identity writes in these tests')
    for name in ('asyncpg.connect','asyncpg.create_pool','bot.db.pg.init_pool','bot.db.pg.execute'):
        monkeypatch.setattr(name,reject)


@pytest.fixture
def catalog(monkeypatch):
    monkeypatch.setattr(pg,'fetch',AsyncMock(return_value=[{'id':10}]))
    async def row(sql,cid):
        return {'id':cid,'name':'Пример','address':'Туран 6/1','parent_complex_id':None}
    monkeypatch.setattr(pg,'fetchrow',AsyncMock(side_effect=row))
    monkeypatch.setattr(shared,'canonical_complex_id',AsyncMock(side_effect=lambda cid:cid))
    monkeypatch.setattr(shared,'resolve_complex_name',AsyncMock(return_value=10))
    monkeypatch.setattr(houses,'get_umbrella_children',AsyncMock(return_value=[]))
    monkeypatch.setattr(houses,'resolve_house',AsyncMock(return_value=None))


@pytest.mark.asyncio
async def test_unknown_source_name_cannot_reuse_url_binding_or_invent_alias(catalog,monkeypatch):
    monkeypatch.setattr(shared,'resolve_complex_name',AsyncMock(return_value=None))
    result=await identity.resolve_rental_complex(complex_url=URL,complex_name='Новое неподтверждённое имя',return_decision=True)
    assert result==dict(complex_id=None,status='unresolved',reason='source_name_unverified',source_canonical_id=10,name_canonical_id=None)
    assert await identity.resolve_rental_complex(complex_url=URL,complex_name='Новое неподтверждённое имя') is None
    houses.get_umbrella_children.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('name',[None,'','  '])
async def test_explicitly_missing_source_name_is_not_inherited_from_catalog(catalog,name):
    result=await identity.resolve_rental_complex(complex_url=URL,complex_name=name,return_decision=True)
    assert result['complex_id'] is None and result['reason']=='source_name_missing'
    pg.fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_known_conflicting_source_name_returns_negative_decision(catalog,monkeypatch):
    monkeypatch.setattr(shared,'resolve_complex_name',AsyncMock(return_value=99))
    result=await identity.resolve_rental_complex(complex_url=URL,complex_name='Другой ЖК',return_decision=True)
    assert result==dict(complex_id=None,status='conflict',reason='source_name_conflict',source_canonical_id=10,name_canonical_id=99)


@pytest.mark.asyncio
async def test_legacy_scalar_and_opt_in_decision_keep_verified_binding(catalog):
    assert await identity.resolve_rental_complex(complex_url=URL,complex_name='Пример',address='Туран 6/1')==10
    result=await identity.resolve_rental_complex(complex_url=URL,complex_name='Пример',return_decision=True)
    assert result['complex_id']==10 and result['status']=='resolved'


@pytest.mark.asyncio
async def test_same_street_different_full_catalog_house_is_a_conflict(catalog):
    result=await identity.resolve_rental_complex(complex_url=URL,complex_name='Пример',address='Туран 6/2',return_decision=True)
    assert result['complex_id'] is None and result['reason']=='source_address_conflict'


@pytest.mark.asyncio
async def test_unresolved_street_spelling_does_not_create_address_alias(catalog):
    result=await identity.resolve_rental_complex(complex_url=URL,complex_name='Пример',address='Неизвестная улица 6/2',return_decision=True)
    assert result['complex_id']==10 and result['reason']=='source_url_and_name_agree'


@pytest.mark.asyncio
async def test_source_database_failure_propagates_instead_of_looking_like_negative_observation(catalog,monkeypatch):
    monkeypatch.setattr(pg,'fetch',AsyncMock(side_effect=RuntimeError('database unavailable')))
    with pytest.raises(RuntimeError,match='database unavailable'):
        await identity.resolve_rental_complex(complex_url=URL,complex_name='Пример',return_decision=True)


@pytest.mark.asyncio
async def test_name_ambiguity_is_uncertainty_not_an_arbitrary_min_id(catalog,monkeypatch):
    monkeypatch.setattr(shared,'resolve_complex_name',AsyncMock(side_effect=shared.AmbiguousComplexIdentity('Пример',{10,99})))
    result=await identity.resolve_rental_complex(complex_url=URL,complex_name='Пример',return_decision=True)
    assert result['complex_id'] is None and result['reason']=='source_name_ambiguous'


@pytest.mark.asyncio
@pytest.mark.parametrize('method,expected',[('address',11),('token',11),('geo',10),('address_geo',10)])
async def test_umbrella_child_still_requires_explicit_supported_house_evidence(catalog,monkeypatch,method,expected):
    monkeypatch.setattr(houses,'get_umbrella_children',AsyncMock(return_value=[{'id':11,'name':'Пример 1'}]))
    monkeypatch.setattr(houses,'resolve_house',AsyncMock(return_value={'house_id':11,'method':method}))
    async def row(sql,cid):
        return {'id':10,'name':'Пример','parent_complex_id':None} if cid==10 else {'parent_complex_id':10}
    monkeypatch.setattr(pg,'fetchrow',AsyncMock(side_effect=row))
    assert await identity.resolve_rental_complex(complex_url=URL,complex_name='Пример')==expected


@pytest.mark.asyncio
async def test_source_child_name_and_other_resolved_house_are_not_silently_combined(catalog,monkeypatch):
    monkeypatch.setattr(shared,'resolve_complex_name',AsyncMock(return_value=11))
    monkeypatch.setattr(houses,'get_umbrella_children',AsyncMock(return_value=[{'id':11,'name':'Пример 1'},{'id':12,'name':'Пример 2'}]))
    monkeypatch.setattr(houses,'resolve_house',AsyncMock(return_value={'house_id':12,'method':'address'}))
    async def row(sql,cid):
        return {'id':10,'name':'Пример','parent_complex_id':None} if cid==10 else {'parent_complex_id':10}
    monkeypatch.setattr(pg,'fetchrow',AsyncMock(side_effect=row))
    result=await identity.resolve_rental_complex(complex_url=URL,complex_name='Пример 1',return_decision=True)
    assert result['complex_id'] is None and result['reason']=='source_name_house_conflict'


@pytest.mark.asyncio
async def test_house_token_or_geo_cannot_override_contradictory_fractional_address():
    child={'id':11,'name':'Пример 1','address':'Туран 6/1','lat':51.128,'lon':71.430}
    result=await houses.resolve_house(umbrella_id=10,umbrella_name='Пример',listing_address='Туран 6/2',
        listing_title='Корпус 1',listing_description=None,listing_lat=51.128,listing_lon=71.430,children=[child])
    assert result is None
