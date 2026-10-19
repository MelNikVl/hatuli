"""A fresh structural source hint replaces a disproved FK, absence does not.

The actual PostgreSQL UPSERT's identity CASE assignments are also executed
against a private in-memory SQLite table; no production/source connection.
"""
import re
import sqlite3
from unittest.mock import AsyncMock

import asyncpg
from bs4 import BeautifulSoup
import pytest

from bot.core import rental_parser as parser
from bot.db import pg


def card(extra="", *, title="2-комнатная квартира, 50 м², ЖК Название из текста"):
    return BeautifulSoup(f'<div class="a-card" data-id="123456">'
        f'<a class="a-card__title" href="/a/show/123456">{title}</a>'
        '<div class="a-card__price">200 000 ₸ в месяц</div>'
        f'<div class="a-card__subtitle">Example 1</div>{extra}</div>',"html.parser").select_one('.a-card')


def test_structural_card_link_is_observed_but_title_regex_or_external_link_is_not():
    explicit = parser._parse_card(card('<a href="/complex/show/55">ЖК Новый проект</a>'),"apartment")
    assert explicit.complex_source_observed and explicit.complex_name == "Новый проект"
    assert explicit.complex_url == "https://krisha.kz/complex/show/55"
    for source in (card(),card('<a href="https://evil.example/complex/show/55">Чужой</a>')):
        inferred = parser._parse_card(source,"apartment")
        assert not inferred.complex_source_observed and inferred.complex_url is None


@pytest.fixture(autouse=True)
def no_external_database(monkeypatch):
    async def deny(*a,**k):
        raise AssertionError("No external DB in rental list identity tests")
    for module, fields in ((asyncpg,('connect','create_pool')),(pg,('init_pool','fetch','fetchrow','fetchval','execute'))):
        for key in fields:
            monkeypatch.setattr(module,key,deny)


def apply_actual_identity_assignments(call, *, old_name="Старый проект", old_url="https://krisha.kz/complex/show/10", old_id=10):
    assignments = []
    for field in ('complex_name','complex_url','complex_id'):
        part = re.search(rf"\b{field} = CASE WHEN \$21::boolean\s+THEN .*? END",call.args[0],re.S)
        assert part, f"{field} must use the source-observation gate"
        assignments.append(part[0].replace('$21::boolean',':observed'))
    with sqlite3.connect(':memory:') as db:
        db.create_function('btrim',1,lambda value:value.strip() if isinstance(value,str) else value)
        db.execute('CREATE TABLE rental_listings (id TEXT PRIMARY KEY,complex_name TEXT,complex_url TEXT,complex_id INTEGER)')
        db.execute('INSERT INTO rental_listings VALUES (?,?,?,?)',('123456',old_name,old_url,old_id))
        db.execute('INSERT INTO rental_listings VALUES (:id,:name,:url,:cid) ON CONFLICT(id) DO UPDATE SET '
            + ','.join(assignments), {'id':call.args[1],'name':call.args[11],
                'url':call.args[16],'cid':call.args[19],'observed':call.args[21]})
        return db.execute('SELECT complex_name,complex_url,complex_id FROM rental_listings').fetchone()


@pytest.mark.asyncio
@pytest.mark.parametrize('status,cid',[('resolved',55),('unresolved',None),('conflict',None)])
async def test_fresh_structural_source_replaces_or_clears_old_fk_atomically_with_its_raw_name_url(monkeypatch,status,cid):
    resolver = AsyncMock(return_value={'status':status,'complex_id':cid})
    monkeypatch.setattr(parser,'resolve_rental_complex',resolver)
    write = AsyncMock(return_value={'is_new':False,'reactivated':False})
    monkeypatch.setattr(pg,'fetchrow',write)
    listing = parser._parse_card(card('<a href="/complex/show/55">ЖК Новый проект</a>'),"apartment")
    assert await parser.save_rental_listings([listing]) == 1
    assert resolver.await_args.kwargs['return_decision'] is True
    assert apply_actual_identity_assignments(write.await_args) == ('Новый проект','https://krisha.kz/complex/show/55',cid)
    # The guarded update is part of the same existing atomic observation SQL.
    assert 'INSERT INTO rental_observations' in write.await_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize('old_name',[None,'Старый проект'])
async def test_absent_structural_hint_cannot_clear_old_identity_or_pair_old_url_with_regex_name(monkeypatch,old_name):
    resolver = AsyncMock(side_effect=AssertionError('No source hint to resolve'))
    monkeypatch.setattr(parser,'resolve_rental_complex',resolver)
    write = AsyncMock(return_value={'is_new':False,'reactivated':False})
    monkeypatch.setattr(pg,'fetchrow',write)
    listing = parser._parse_card(card(),"apartment")
    assert listing.complex_name and not listing.complex_source_observed
    assert await parser.save_rental_listings([listing]) == 1
    assert apply_actual_identity_assignments(write.await_args,old_name=old_name) == (old_name,'https://krisha.kz/complex/show/10',10)
    assert write.await_args.args[16] is None and write.await_args.args[21] is False
    resolver.assert_not_awaited()


@pytest.mark.asyncio
async def test_source_flag_cannot_turn_an_invalid_external_url_into_an_observation(monkeypatch):
    resolver = AsyncMock(side_effect=AssertionError('Invalid origin'))
    monkeypatch.setattr(parser,'resolve_rental_complex',resolver)
    write = AsyncMock(return_value={'is_new':False,'reactivated':False})
    monkeypatch.setattr(pg,'fetchrow',write)
    listing = parser._parse_card(card(),"apartment")
    listing.complex_url = 'https://evil.example/complex/show/55'
    listing.complex_source_observed = True
    assert await parser.save_rental_listings([listing]) == 1
    assert apply_actual_identity_assignments(write.await_args)[2] == 10
    resolver.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_resolver_decision_does_not_write_or_acknowledge_page_observation(monkeypatch):
    monkeypatch.setattr(parser,'resolve_rental_complex',AsyncMock(return_value=None))
    write = AsyncMock()
    monkeypatch.setattr(pg,'fetchrow',write)
    listing = parser._parse_card(card('<a href="/complex/show/55">ЖК Новый проект</a>'),"apartment")
    assert await parser.save_rental_listings([listing]) == 0
    write.assert_not_awaited()
