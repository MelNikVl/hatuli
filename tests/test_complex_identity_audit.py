"""The server audit reads proposals without applying them or running migrations."""
import os
from uuid import uuid4

import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def db():
    from bot.db.pg import close_pool, init_pool
    await init_pool(os.environ.get('DATABASE_URL', 'postgresql://krisha:123@localhost/krisha_bot'))
    try:
        yield
    finally:
        await close_pool()


@pytest.mark.asyncio
async def test_audit_does_not_apply_canonical_or_migrations(db, monkeypatch):
    from bot.db import pg
    from scripts.audit_complex_identity_current import audit

    name = '__test_cx_audit_' + uuid4().hex + '__'
    canonical = await pg.fetchval("""
        INSERT INTO complexes (name, krisha_url, is_newbuild)
        VALUES ($1, $2, TRUE) RETURNING id
    """, name, 'https://krisha.kz/complex/show/astana/' + name + '/')
    alias = await pg.fetchval("""
        INSERT INTO complexes (name, krisha_url) VALUES ($1, $2) RETURNING id
    """, 'ЖК ' + name, 'https://krisha.kz/complex/show/astana/' + name + '/')
    before = await pg.fetchval('SELECT count(*) FROM schema_migrations')

    async def forbidden_pool(*args, **kwargs):
        raise AssertionError('read-only audit must not use the migration-applying pool initializer')

    monkeypatch.setattr(pg, 'init_pool', forbidden_pool)
    try:
        report = await audit(os.environ.get('DATABASE_URL', 'postgresql://krisha:123@localhost/krisha_bot'),
                             binding_preview=True)
        assert report['read_only'] is True
        proposed = {row['id']: row for row in report['proposed_canonical_changes']}
        assert proposed[alias]['proposed_canonical_id'] == canonical
        assert await pg.fetchval('SELECT canonical_id FROM complexes WHERE id=$1', alias) is None
        assert await pg.fetchval('SELECT count(*) FROM schema_migrations') == before
        assert 'binding_preview_for_current_canonical_map' in report
    finally:
        await pg.execute('DELETE FROM complexes WHERE id = ANY($1::int[])', [alias, canonical])
