"""Rental address enrichment must not manufacture a source complex name."""
from unittest.mock import AsyncMock

import pytest

from bot.core.rebind import bind_by_address


@pytest.fixture(autouse=True)
def no_database(monkeypatch):
    monkeypatch.setenv('DATABASE_URL','postgresql://unused:unused@127.0.0.1:1/rebind_identity_guard')
    async def reject(*args,**kwargs):
        raise AssertionError('No PostgreSQL access in source-name regression tests')
    for name in ('asyncpg.connect','asyncpg.create_pool','bot.db.pg.init_pool'):
        monkeypatch.setattr(name,reject)


@pytest.mark.asyncio
async def test_rental_address_rebind_is_noop_without_reading_or_synthesizing_source_name(monkeypatch):
    fetch,write=AsyncMock(),AsyncMock()
    monkeypatch.setattr('bot.db.pg.fetch',fetch)
    monkeypatch.setattr('bot.db.pg.execute',write)
    assert await bind_by_address('rental_listings')==0
    fetch.assert_not_awaited();write.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_sale_admin_address_branch_is_unchanged(monkeypatch):
    # Scope is deliberately rental only. No live legacy rebind is executed.
    fetch=AsyncMock(side_effect=[[{'naddr':'туран 6','complex_name':'Каталог'}],
                                [{'id':'synthetic-sale','naddr':'туран 6'}]])
    write=AsyncMock()
    monkeypatch.setattr('bot.db.pg.fetch',fetch)
    monkeypatch.setattr('bot.db.pg.execute',write)
    assert await bind_by_address('apartment_listings')==1
    assert write.await_args.args==('UPDATE apartment_listings SET complex_name = $2 WHERE id = $1','synthetic-sale','Каталог')


@pytest.mark.asyncio
async def test_unapproved_table_name_remains_rejected():
    with pytest.raises(ValueError):await bind_by_address('untrusted_table')
