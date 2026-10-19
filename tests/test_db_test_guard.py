"""An unsafe or missing database configuration must never initialize a pool."""
from unittest.mock import AsyncMock

import pytest

from tests.db_test_guard import init_test_pool, require_test_database_url


@pytest.mark.asyncio
@pytest.mark.parametrize('environ', [
    {},
    {'DATABASE_URL': ''},
    {'DATABASE_URL': 'postgresql://user:secret@localhost/'},
    {'DATABASE_URL': 'postgresql://user:secret@localhost/krisha_bot'},
    {'DATABASE_URL': 'postgresql://user:secret@localhost/contest'},
    {'DATABASE_URL': 'postgresql://user:secret@localhost/latest'},
    {'DATABASE_URL': 'sqlite:///hatuli_test'},
    {'DATABASE_URL': 'postgresql://user:secret@localhost/hatuli_test/krisha_bot'},
    {'DATABASE_URL': 'postgresql://user:secret@localhost/hatuli_test?database=krisha_bot'},
    {'DATABASE_URL': 'postgresql://user:secret@localhost/hatuli_test?dbname=krisha_bot'},
    {'HATULI_TEST_DATABASE_URL': 'postgresql://user:secret@localhost/krisha_bot',
     'DATABASE_URL': 'postgresql://user:secret@localhost/hatuli_test'},
])
async def test_unsafe_configuration_cannot_initialize_pool(environ, monkeypatch):
    initialize = AsyncMock()
    monkeypatch.setattr('bot.db.pg.init_pool', initialize)
    with pytest.raises(RuntimeError, match='Refusing database integration tests') as error:
        await init_test_pool(environ)
    initialize.assert_not_awaited()
    assert 'secret' not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize('database', [
    'hatuli_test', 'hatuli_audit_test_c7fd0b9f', 'test_foo', 'foo_test', 'test',
])
async def test_explicit_test_database_initializes_pool(database, monkeypatch):
    dsn = 'postgresql://user:secret@localhost/' + database
    pool = object()
    initialize = AsyncMock(return_value=pool)
    monkeypatch.setattr('bot.db.pg.init_pool', initialize)
    assert await init_test_pool({'DATABASE_URL': dsn}) is pool
    initialize.assert_awaited_once_with(dsn)


def test_explicit_test_url_takes_priority_over_production_environment():
    test_dsn = 'postgresql://user:secret@localhost/hatuli_test?sslmode=require'
    assert require_test_database_url({
        'HATULI_TEST_DATABASE_URL': test_dsn,
        'DATABASE_URL': 'postgresql://user:secret@localhost/krisha_bot',
    }) == test_dsn
