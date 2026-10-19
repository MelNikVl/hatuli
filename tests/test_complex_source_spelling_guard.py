"""Execute source spelling SELECTs against a private in-memory SQLite catalog.

No PostgreSQL connection, migrations, production fixtures or external source.
"""
import sqlite3

import pytest

from bot.core.complex_ingest_identity import (
    AmbiguousComplexIdentity, normalize_complex_name, resolve_complex_name,
)


class MemoryCatalog:
    def __init__(self):
        self.db=sqlite3.connect(':memory:')
        self.db.row_factory=sqlite3.Row
        self.db.create_function('complex_name_key',1,normalize_complex_name)
        self.db.executescript('''
          CREATE TABLE complexes(id integer,name text,canonical_id integer,canonical_reason text,
                                 is_garbage boolean default false,is_street boolean default false);
          CREATE TABLE complex_aliases(complex_id integer,normalized_name text,status text);
          INSERT INTO complexes(id,name) VALUES(10,'Канонический ЖК'),(20,'Другой ЖК');
          INSERT INTO complexes(id,name,canonical_id) VALUES(11,'Старое неподтверждённое имя',10);
        ''')
    async def fetch(self,sql,*args):
        assert sql.lstrip().upper().startswith('SELECT')
        return [dict(row) for row in self.db.execute(sql.replace('$1','?1'),args).fetchall()]
    async def fetchrow(self,sql,*args):
        values=await self.fetch(sql,*args)
        return values[0] if values else None
    def alias(self,cid,name,status):
        self.db.execute('INSERT INTO complex_aliases VALUES(?,?,?)',(cid,normalize_complex_name(name),status))


@pytest.fixture
def catalog(monkeypatch):
    monkeypatch.setenv('DATABASE_URL','postgresql://unused:unused@127.0.0.1:1/source_spelling')
    async def reject(*args,**kwargs):raise AssertionError('PostgreSQL forbidden')
    for name in ('asyncpg.connect','asyncpg.create_pool','bot.db.pg.init_pool'):
        monkeypatch.setattr(name,reject)
    value=MemoryCatalog()
    try:yield value
    finally:value.db.close()


@pytest.mark.asyncio
async def test_canonical_name_remains_a_valid_source_spelling(catalog):
    assert await resolve_complex_name('ЖК «Канонический ЖК»',strict=True,canonical_names_only=True,connection=catalog)==10


@pytest.mark.asyncio
async def test_redirect_alone_cannot_approve_a_fresh_source_name_but_legacy_mode_is_unchanged(catalog):
    name='Старое неподтверждённое имя'
    assert await resolve_complex_name(name,strict=True,connection=catalog)==10
    assert await resolve_complex_name(name,strict=True,canonical_names_only=True,connection=catalog) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('status',['observed','rejected','verified'])
async def test_only_reviewed_alias_can_confirm_redirect_spelling(catalog,status):
    name='Старое неподтверждённое имя';catalog.alias(11,name,status)
    result=await resolve_complex_name(name,strict=True,canonical_names_only=True,connection=catalog)
    assert result==(10 if status=='verified' else None)


@pytest.mark.asyncio
async def test_shared_verified_alias_remains_ambiguous(catalog):
    for cid in (10,20):catalog.alias(cid,'Общее имя','verified')
    with pytest.raises(AmbiguousComplexIdentity):
        await resolve_complex_name('Общее имя',strict=True,canonical_names_only=True,connection=catalog)


@pytest.mark.asyncio
async def test_prefix_or_unknown_spelling_does_not_bind(catalog):
    assert await resolve_complex_name('Канонический ЖК 2',strict=True,canonical_names_only=True,connection=catalog) is None
