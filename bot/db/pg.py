"""
PostgreSQL connection pool (asyncpg).

Usage:
    from bot.db.pg import init_pool, get_pool, execute, fetch, fetchrow, fetchval

    await init_pool(dsn)   # once at startup
    rows = await fetch("SELECT ...")
    await execute("INSERT ...")
    await close_pool()     # at shutdown
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

# Фиксированный ключ advisory-lock'а, чтобы параллельно стартующие сервисы
# не применяли одни и те же миграции одновременно.
_MIGRATION_LOCK_KEY = 728140


async def _apply_migrations() -> None:
    """Применяет неприменённые файлы migrations/*.sql (идемпотентные)."""
    async with get_pool().acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                name       TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ DEFAULT now()
            )
        """)
        await conn.fetchval("SELECT pg_advisory_lock($1)", _MIGRATION_LOCK_KEY)
        try:
            applied = {r["name"] for r in
                       await conn.fetch("SELECT name FROM schema_migrations")}
            for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if path.name in applied:
                    continue
                sql = path.read_text(encoding="utf-8")
                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute(
                        "INSERT INTO schema_migrations (name) VALUES ($1)",
                        path.name)
                logger.info("migration applied: %s", path.name)
        finally:
            await conn.fetchval("SELECT pg_advisory_unlock($1)", _MIGRATION_LOCK_KEY)


async def init_pool(dsn: str) -> asyncpg.Pool:
    global _pool
    _pool = await asyncpg.create_pool(dsn, min_size=2, max_size=10, command_timeout=30)
    logger.info("PostgreSQL pool created (%s)", dsn.split("@")[-1])
    await _apply_migrations()
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("PostgreSQL pool not initialised — call init_pool() first")
    return _pool


async def execute(sql: str, *args: Any) -> str:
    async with get_pool().acquire() as conn:
        return await conn.execute(sql, *args)


async def fetch(sql: str, *args: Any) -> list[asyncpg.Record]:
    async with get_pool().acquire() as conn:
        return await conn.fetch(sql, *args)


async def fetchrow(sql: str, *args: Any) -> asyncpg.Record | None:
    async with get_pool().acquire() as conn:
        return await conn.fetchrow(sql, *args)


async def fetchval(sql: str, *args: Any) -> Any:
    async with get_pool().acquire() as conn:
        return await conn.fetchval(sql, *args)
