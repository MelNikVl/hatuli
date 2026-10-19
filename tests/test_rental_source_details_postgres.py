"""Migration contract on explicitly opted-in disposable PostgreSQL CI only."""
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
import uuid

import pytest

DSN = os.getenv("HATULI_RENTAL_STAGE0_TEST_DATABASE_URL")
PLACEHOLDER = bool(DSN and urlsplit(DSN).port == 1)


@pytest.mark.skipif(not DSN or PLACEHOLDER,reason="requires explicitly disposable PostgreSQL CI database, not a placeholder")
def test_rental_114_is_nullable_idempotent_and_never_backfills_source_facts():
    parsed = urlsplit(DSN)
    assert parsed.hostname in ("127.0.0.1","localhost","::1")
    assert parsed.path == "/hatuli_test", "Only the disposable CI database is allowed"

    async def run():
        import asyncpg
        connection = await asyncpg.connect(DSN)
        schema = "qa_rental_details_"+uuid.uuid4().hex
        try:
            assert await connection.fetchval("SELECT current_database()") == "hatuli_test"
            await connection.execute(f'CREATE SCHEMA "{schema}"')
            await connection.execute(f'SET search_path TO "{schema}"')
            await connection.execute("CREATE TABLE rental_listings(id text PRIMARY KEY,description_checked_at timestamptz)")
            old = datetime(2026,10,13,tzinfo=timezone.utc)
            await connection.execute("INSERT INTO rental_listings VALUES ('legacy',$1),('unknown',NULL)",old)
            sql = (Path(__file__).resolve().parents[1]/"migrations/114_rental_source_details.sql").read_text(encoding="utf-8")
            await connection.execute(sql); await connection.execute(sql)
            rows = await connection.fetch("SELECT * FROM rental_listings ORDER BY id")
            assert all(row["source_details"] is None for row in rows)
            assert rows[0]["description_checked_at"] == old and rows[1]["description_checked_at"] is None
            column = await connection.fetchrow("SELECT is_nullable,column_default FROM information_schema.columns WHERE table_schema=$1 AND table_name='rental_listings' AND column_name='source_details'",schema)
            assert column["is_nullable"] == "YES" and column["column_default"] is None
            with pytest.raises(asyncpg.CheckViolationError):
                await connection.execute("UPDATE rental_listings SET source_details='[]'::jsonb WHERE id='legacy'")
            with pytest.raises(asyncpg.CheckViolationError):
                await connection.execute("UPDATE rental_listings SET source_details=$1::jsonb WHERE id='legacy'",json.dumps({"oversized":"x"*17000}))
            await connection.execute("UPDATE rental_listings SET source_details=$1::jsonb WHERE id='legacy'",json.dumps({"version":1,"year_built":2020}))
            assert json.loads(await connection.fetchval("SELECT source_details::text FROM rental_listings WHERE id='legacy'"))["year_built"] == 2020
            assert await connection.fetchval("SELECT description_checked_at FROM rental_listings WHERE id='legacy'") == old
        finally:
            await connection.execute("SET search_path TO public")
            await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            await connection.close()
    asyncio.run(run())
