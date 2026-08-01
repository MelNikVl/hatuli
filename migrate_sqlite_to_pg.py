#!/usr/bin/env python3
"""
Migrate SQLite → PostgreSQL.
Fixes: datetime strings, int→bool columns, unknown columns.
"""
import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

SQLITE_PATH = os.getenv("DB_PATH", "bot.db")
DATABASE_URL = os.getenv("DATABASE_URL", "")

TABLES = [
    "users", "listings", "investment_listings",
    "events", "bot_requests", "parse_errors",
    "favorites", "blocked_listings", "saved_searches",
    "user_listing_notifications", "listing_views",
    "ai_cache", "user_listings",
]


def _parse_dt(val):
    if not isinstance(val, str):
        return val
    for fmt in [
        "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f",   "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",   "%Y-%m-%d %H:%M:%S",
    ]:
        try:
            dt = datetime.strptime(val, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return val


async def get_pg_columns(conn, table: str) -> dict[str, str]:
    """Return {column_name: data_type} for PG table."""
    rows = await conn.fetch(
        """
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_name = $1 AND table_schema = 'public'
        """,
        table,
    )
    return {r["column_name"]: r["data_type"] for r in rows}


def _coerce(val, pg_type: str):
    """Coerce SQLite value to match PostgreSQL column type."""
    val = _parse_dt(val)
    if pg_type == "boolean" and isinstance(val, int):
        return bool(val)
    return val


async def migrate() -> None:
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL not set in .env")

    import asyncpg
    pg = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=3)

    if not os.path.exists(SQLITE_PATH):
        logger.warning("SQLite %s not found", SQLITE_PATH)
        await pg.close()
        return

    sqlite_conn = sqlite3.connect(SQLITE_PATH)
    sqlite_conn.row_factory = sqlite3.Row

    existing_sqlite = {
        r[0] for r in sqlite_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }

    for table in TABLES:
        if table not in existing_sqlite:
            logger.info("%-35s not in SQLite — skip", table)
            continue

        rows = sqlite_conn.execute(f"SELECT * FROM {table}").fetchall()
        if not rows:
            logger.info("%-35s empty", table)
            continue

        sqlite_cols = [d[0] for d in sqlite_conn.execute(
            f"SELECT * FROM {table} LIMIT 1"
        ).description]

        async with pg.acquire() as conn:
            pg_col_types = await get_pg_columns(conn, table)

        valid_cols = [c for c in sqlite_cols if c in pg_col_types]
        if not valid_cols:
            logger.warning("%-35s no matching columns — skip", table)
            continue

        skipped = set(sqlite_cols) - set(valid_cols)
        if skipped:
            logger.info("%-35s skipping: %s", table, skipped)

        col_indices = [sqlite_cols.index(c) for c in valid_cols]
        placeholders = ", ".join(f"${i+1}" for i in range(len(valid_cols)))
        col_str = ", ".join(valid_cols)
        sql = f"INSERT INTO {table} ({col_str}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"

        migrated = errors = 0
        async with pg.acquire() as conn:
            for row in rows:
                values = [
                    _coerce(row[idx], pg_col_types[col])
                    for idx, col in zip(col_indices, valid_cols)
                ]
                try:
                    await conn.execute(sql, *values)
                    migrated += 1
                except Exception as e:
                    errors += 1
                    if errors <= 2:
                        logger.warning("  [%s] row error: %s", table, str(e)[:150])

        logger.info("%-35s migrated %d rows, %d errors", table, migrated, errors)

    sqlite_conn.close()
    await pg.close()
    logger.info("Migration complete!")


if __name__ == "__main__":
    asyncio.run(migrate())
