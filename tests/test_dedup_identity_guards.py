"""Pure negative-evidence regressions; no database, images or source calls."""
from copy import deepcopy

import pytest

from bot.core.dedup_listings import find_duplicates
from bot.core.house_resolution import conflicting_house_addresses


@pytest.fixture(autouse=True)
def no_database(monkeypatch):
    monkeypatch.setenv('DATABASE_URL', 'postgresql://unused:unused@127.0.0.1:1/identity_guards')
    async def reject(*args, **kwargs):
        raise AssertionError('Identity guard tests must not connect to PostgreSQL')
    monkeypatch.setattr('asyncpg.connect', reject)
    monkeypatch.setattr('asyncpg.create_pool', reject)
    monkeypatch.setattr('bot.db.pg.init_pool', reject)


def listing(identifier, **changes):
    row=dict(id=str(identifier), url=f'https://krisha.kz/a/show/{identifier}',
                address='Туран 6/1', rooms=2, area=60., price=30_000_000,
                floor=5, lat=51.128, lon=71.430, is_owner=False)
    row.update(changes)
    return row


@pytest.mark.parametrize('first,second',[
    ('Туран 6/1','Туран 6/2'),
    ('улица Туран, 6Б','Туран 6'),
    ('Туран 6','Туран 16'),
    ('Астана, проспект Туран, 6/1 — парк','Туран 6/2'),
    ('Туран уч. 6','Туран уч. 6/1'),
])
def test_full_number_or_letter_is_negative_even_with_identical_geo(first,second):
    assert conflicting_house_addresses(first,second)
    rows=[listing(100001),listing(100002)]
    rows[0]['address']=first;rows[1]['address']=second
    before=deepcopy(rows)
    assert find_duplicates(rows)==({}, {})
    assert rows==before


@pytest.mark.parametrize('first,second',[
    ('Туран 6/1','проспект Туран, 6/1'),
    ('Туран 6/1','Другая улица 6/2'),
    ('Туран','Туран 6/1'),
    ('уч. 6','Туран уч. 6/1'),
    ('Туран уч. 6','Туран 6'),
    (None,'Туран 6/1'),
])
def test_unknown_address_parts_do_not_invent_a_conflict_or_alias(first,second):
    assert conflicting_house_addresses(first,second) is False


@pytest.mark.parametrize('changed,value',[('floor',6),('rooms',3),('area',70),('address','Туран 6/2')])
def test_shared_photo_identifier_cannot_override_known_unit_conflict(changed,value):
    rows=[listing(100001),listing(100002)]
    for row in rows:row['url']='https://example.test/11111111-2222-3333-4444-555555555555/image.jpg'
    rows[1][changed]=value
    assert find_duplicates(rows)==({}, {})


def test_same_house_and_unit_fields_still_find_existing_address_rule():
    duplicates,rules=find_duplicates([listing(100001),listing(100002)])
    assert duplicates=={'100002':'100001'} and rules=={'100002':'addr_area'}


def test_owner_preference_is_retained_for_compatible_unit():
    rows=[listing(100001),listing(100002)];rows[1]['is_owner']=True
    assert find_duplicates(rows)[0]=={'100001':'100002'}


def test_geo_fallback_with_unresolved_street_spelling_is_not_rewritten_by_this_guard():
    rows=[listing(100001),listing(100002)];rows[1]['address']='Непроверенное другое написание 6/1'
    duplicates,rules=find_duplicates(rows)
    assert duplicates=={'100002':'100001'} and rules=={'100002':'geo'}


def test_owner_reparenting_does_not_transfer_known_other_house_to_new_primary():
    rows=[listing(100001),listing(100002),listing(100003)]
    rows[0]['address']=None
    rows[1]['address']='Туран 6/1'
    rows[2]['address']='Туран 6/2';rows[2]['is_owner']=True
    duplicates,_=find_duplicates(rows)
    assert duplicates.get('100002')!='100003'
