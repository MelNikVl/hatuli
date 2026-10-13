"""Exercise login races on an explicitly selected, disposable PostgreSQL database.

This suite never uses DATABASE_URL, pg.init_pool or project migrations. The
caller creates a database named hatuli_auth_test_<id> and passes its URL through
HATULI_AUTH_TEST_DATABASE_URL. Each test owns a fresh minimal schema.
"""
from __future__ import annotations

import asyncio
import os
import re
import uuid
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

import asyncpg
import pytest
import pytest_asyncio

from bot.core import site_auth


def isolated_auth_dsn() -> str:
    dsn = os.environ.get("HATULI_AUTH_TEST_DATABASE_URL", "")
    if not dsn:
        pytest.skip("Requires an explicit disposable auth test database")
    parsed = urlsplit(dsn)
    database = unquote(parsed.path.lstrip("/"))
    if (parsed.scheme not in ("postgres", "postgresql")
            or not re.fullmatch(r"hatuli_auth_test_[a-z0-9_]{1,48}", database)
            or parsed.query or parsed.fragment):
        pytest.fail("Auth SQL tests require a dedicated hatuli_auth_test_<id> URL")
    return dsn


@pytest_asyncio.fixture
async def auth_sql(monkeypatch):
    dsn = isolated_auth_dsn()
    schema = "hatuli_auth_sql_" + uuid.uuid4().hex
    connection = await asyncpg.connect(dsn, command_timeout=15)
    pool = None
    schema_created = False
    try:
        await connection.execute(f'CREATE SCHEMA "{schema}"')
        schema_created = True
        await connection.execute(f'SET search_path TO "{schema}"')
        await connection.execute("""
            CREATE TABLE users (
                user_id BIGINT PRIMARY KEY,
                is_blocked INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE login_tokens (
                token TEXT PRIMARY KEY,
                telegram_id BIGINT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMPTZ DEFAULT now(),
                verified_at TIMESTAMPTZ,
                consumed_at TIMESTAMPTZ
            );
            CREATE TABLE site_sessions (
                session_id TEXT PRIMARY KEY,
                user_id BIGINT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT now()
            );
            INSERT INTO users (user_id) VALUES (101), (202);
        """)

        async def initialize_test_connection(conn):
            await conn.execute(f'SET search_path TO "{schema}"')

        pool = await asyncpg.create_pool(
            dsn, min_size=2, max_size=8, command_timeout=15,
            setup=initialize_test_connection,
        )

        async def fetchrow(sql, *args):
            async with pool.acquire() as conn:
                return await conn.fetchrow(sql, *args)

        async def execute(sql, *args):
            async with pool.acquire() as conn:
                return await conn.execute(sql, *args)

        monkeypatch.setattr(site_auth, "fetchrow", fetchrow)
        monkeypatch.setattr(site_auth, "execute", execute)
        yield SimpleNamespace(pool=pool, execute=execute, fetchrow=fetchrow)
    finally:
        if pool is not None:
            await pool.close()
        if schema_created:
            await connection.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await connection.close()


async def pending_login(db, *, owner=None, status="pending", age_minutes=0):
    token = "original_" + uuid.uuid4().hex
    await db.execute("""
        INSERT INTO login_tokens(token, telegram_id, status, created_at)
        VALUES ($1, $2, $3, now() - $4::int * interval '1 minute')
    """, token, owner, status, age_minutes)
    return token


async def issued_pair(db):
    original = await pending_login(db)
    callback = await site_auth.verify_login_and_create_return_token(original, 101)
    assert callback and callback != original
    return original, callback


@pytest.mark.asyncio
async def test_claim_race_between_telegram_accounts_keeps_one_owner(auth_sql):
    original = await pending_login(auth_sql)
    outcomes = await asyncio.gather(
        site_auth.verify_login_and_create_return_token(original, 101),
        site_auth.verify_login_and_create_return_token(original, 202),
    )
    assert sum(bool(result) for result in outcomes) == 1
    owner = 101 if outcomes[0] else 202
    record = await auth_sql.fetchrow("SELECT * FROM login_tokens WHERE token=$1", original)
    assert record["telegram_id"] == owner and record["status"] == "verified"
    callback = next(result for result in outcomes if result)
    record = await auth_sql.fetchrow("SELECT * FROM login_tokens WHERE token=$1", callback)
    assert record["telegram_id"] == owner and record["status"] == "verified"
    assert (await auth_sql.fetchrow("SELECT count(*) AS n FROM login_tokens"))["n"] == 2


@pytest.mark.asyncio
async def test_duplicate_start_from_same_owner_issues_exactly_one_link(auth_sql):
    original = await pending_login(auth_sql)
    outcomes = await asyncio.gather(*(
        site_auth.verify_login_and_create_return_token(original, 101) for _ in range(6)
    ))
    assert sum(bool(result) for result in outcomes) == 1
    callback = next(result for result in outcomes if result)
    assert await site_auth.verify_login_and_create_return_token(original, 101) is None
    assert await site_auth.verify_login_and_create_return_token(callback, 101) is None
    assert (await auth_sql.fetchrow("SELECT count(*) AS n FROM login_tokens"))["n"] == 2


@pytest.mark.asyncio
async def test_subscription_retry_preserves_first_owner(auth_sql):
    original = await pending_login(auth_sql)
    await site_auth.mark_login_not_subscribed(original, 101)
    await site_auth.mark_login_not_subscribed(original, 202)
    assert await site_auth.verify_login_and_create_return_token(original, 202) is None
    record = await auth_sql.fetchrow("SELECT * FROM login_tokens WHERE token=$1", original)
    assert record["telegram_id"] == 101 and record["status"] == "not_subscribed"
    assert await site_auth.consume_login_token(original) is None
    assert await site_auth.verify_login_and_create_return_token(original, 101)


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["original", "callback"])
async def test_callback_and_poll_remain_independent_in_both_orders(auth_sql, first):
    original, callback = await issued_pair(auth_sql)
    first_token, second_token = (original, callback) if first == "original" else (callback, original)
    first_session = await site_auth.consume_login_token(first_token)
    second_session = await site_auth.consume_login_token(second_token)
    assert first_session and second_session and first_session != second_session
    assert await site_auth.consume_login_token(first_token) is None
    assert await site_auth.consume_login_token(second_token) is None
    counts = await auth_sql.fetchrow("SELECT count(*) AS n, min(user_id) AS owner FROM site_sessions")
    assert counts["n"] == 2 and counts["owner"] == 101


@pytest.mark.asyncio
async def test_callback_and_poll_can_exchange_concurrently(auth_sql):
    original, callback = await issued_pair(auth_sql)
    results = await asyncio.gather(
        site_auth.consume_login_token(original), site_auth.consume_login_token(callback),
    )
    assert all(results) and len(set(results)) == 2
    assert (await auth_sql.fetchrow("SELECT count(*) AS n FROM site_sessions"))["n"] == 2


@pytest.mark.asyncio
async def test_return_link_is_one_use_even_under_concurrent_clicks(auth_sql):
    _, callback = await issued_pair(auth_sql)
    results = await asyncio.gather(*(
        site_auth.consume_login_token(callback) for _ in range(6)
    ))
    assert sum(bool(result) for result in results) == 1
    assert (await auth_sql.fetchrow("SELECT count(*) AS n FROM site_sessions"))["n"] == 1
    # A late legacy verification cannot resurrect a credential already spent.
    await auth_sql.execute("UPDATE login_tokens SET status='verified' WHERE token=$1", callback)
    assert await site_auth.consume_login_token(callback) is None


@pytest.mark.asyncio
async def test_return_link_lifetime_starts_at_confirmation(auth_sql):
    original = await pending_login(auth_sql, age_minutes=9)
    callback = await site_auth.verify_login_and_create_return_token(original, 101)
    assert callback
    rows = await auth_sql.fetchrow("""
        SELECT (SELECT created_at FROM login_tokens WHERE token=$1) AS original_at,
               (SELECT created_at FROM login_tokens WHERE token=$2) AS callback_at
    """, original, callback)
    assert (rows["callback_at"] - rows["original_at"]).total_seconds() > 8 * 60
    # Expiry of the original tab does not expire the newer bot return link.
    await auth_sql.execute(
        "UPDATE login_tokens SET created_at=now()-interval '11 minutes' WHERE token=$1", original,
    )
    assert await site_auth.consume_login_token(original) is None
    assert await site_auth.consume_login_token(callback)


@pytest.mark.asyncio
async def test_expired_original_cannot_be_verified_or_rebound(auth_sql):
    original = await pending_login(auth_sql, age_minutes=11)
    await site_auth.mark_login_not_subscribed(original, 101)
    assert await site_auth.verify_login_and_create_return_token(original, 101) is None
    record = await auth_sql.fetchrow("SELECT * FROM login_tokens WHERE token=$1", original)
    assert record["telegram_id"] is None and record["status"] == "pending"
    assert (await auth_sql.fetchrow("SELECT count(*) AS n FROM login_tokens"))["n"] == 1


@pytest.mark.asyncio
async def test_blocked_user_cannot_receive_return_link(auth_sql):
    original = await pending_login(auth_sql)
    await auth_sql.execute("UPDATE users SET is_blocked=1 WHERE user_id=101")
    assert await site_auth.verify_login_and_create_return_token(original, 101) is None
    assert (await auth_sql.fetchrow("SELECT count(*) AS n FROM login_tokens"))["n"] == 1
    assert (await auth_sql.fetchrow("SELECT count(*) AS n FROM site_sessions"))["n"] == 0


@pytest.mark.asyncio
async def test_blocking_after_confirmation_rejects_both_browser_flows(auth_sql):
    original, callback = await issued_pair(auth_sql)
    await auth_sql.execute("UPDATE users SET is_blocked=1 WHERE user_id=101")
    assert await site_auth.consume_login_token(original) is None
    assert await site_auth.consume_login_token(callback) is None
    assert (await auth_sql.fetchrow("SELECT count(*) AS n FROM site_sessions"))["n"] == 0


@pytest.mark.asyncio
async def test_expired_callback_does_not_break_original_poll(auth_sql):
    original, callback = await issued_pair(auth_sql)
    await auth_sql.execute(
        "UPDATE login_tokens SET created_at=now()-interval '11 minutes' WHERE token=$1", callback,
    )
    assert await site_auth.consume_login_token(callback) is None
    assert await site_auth.consume_login_token(original)


@pytest.mark.asyncio
async def test_deleted_user_cannot_redeem_return_link(auth_sql):
    original, callback = await issued_pair(auth_sql)
    await auth_sql.execute("DELETE FROM users WHERE user_id=101")
    assert await site_auth.consume_login_token(original) is None
    assert await site_auth.consume_login_token(callback) is None
    assert (await auth_sql.fetchrow("SELECT count(*) AS n FROM site_sessions"))["n"] == 0
