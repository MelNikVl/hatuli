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
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


async def init_pool(dsn: str) -> asyncpg.Pool:
    global _pool
    _pool = await asyncpg.create_pool(dsn, min_size=2, max_size=10, command_timeout=30)
    logger.info("PostgreSQL pool created (%s)", dsn.split("@")[-1])
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
