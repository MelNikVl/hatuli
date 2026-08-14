"""DB queries for investment listings."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)


async def upsert_investment_listing(db_path: str, listing: dict, score_data: dict) -> str:
    """
    Insert or update investment listing.
    Returns: 'new', 'updated', or 'unchanged'.
    """
    lid = listing["id"]
    now = datetime.now(timezone.utc).isoformat()
    bd = score_data.get("breakdown", {})
    reasons_json = json.dumps(score_data.get("reasons", []), ensure_ascii=False)

    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("SELECT id, price, price_history FROM investment_listings WHERE id=?", (lid,))
        row = await cursor.fetchone()

        if row is None:
            await db.execute("""
                INSERT INTO investment_listings
                (id, url, title, price, area, address, district, prop_type,
                 yield_pct, est_rent, payback_years,
                 score_total, score_yield, score_location, score_supply_demand,
                 score_liquidity, score_quality, reasons,
                 first_seen, last_seen, price_history, notified)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'[]',0)
            """, (
                lid, listing.get("url"), listing.get("title"), listing.get("price"),
                listing.get("area"), listing.get("address"), listing.get("district"),
                listing.get("prop_type"), listing.get("yield_pct"), listing.get("est_rent"),
                listing.get("payback_years"),
                score_data["total_score"], bd.get("yield",0), bd.get("location",0),
                bd.get("supply_demand",0), bd.get("liquidity",0), bd.get("quality",0),
                reasons_json, now, now,
            ))
            await db.commit()
            return "new"

        old_price = row[1]
        price_history = json.loads(row[2] or "[]")
        new_price = listing.get("price", old_price)
        status = "unchanged"

        if new_price != old_price:
            price_history.append({"price": old_price, "date": now})
            status = "updated"

        await db.execute("""
            UPDATE investment_listings SET
                price=?, yield_pct=?, est_rent=?, payback_years=?,
                score_total=?, score_yield=?, score_location=?,
                score_supply_demand=?, score_liquidity=?, score_quality=?,
                reasons=?, last_seen=?, price_history=?
            WHERE id=?
        """, (
            new_price, listing.get("yield_pct"), listing.get("est_rent"),
            listing.get("payback_years"),
            score_data["total_score"], bd.get("yield",0), bd.get("location",0),
            bd.get("supply_demand",0), bd.get("liquidity",0), bd.get("quality",0),
            reasons_json, now, json.dumps(price_history, ensure_ascii=False), lid,
        ))
        await db.commit()
        return status


async def mark_notified(db_path: str, listing_id: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("UPDATE investment_listings SET notified=1 WHERE id=?", (listing_id,))
        await db.commit()


async def get_top_unnotified(db_path: str, min_score: int = 75, limit: int = 5) -> list[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
            SELECT * FROM investment_listings
            WHERE notified=0 AND score_total >= ?
            ORDER BY score_total DESC
            LIMIT ?
        """, (min_score, limit))
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_all_investment_listings(db_path: str) -> list[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
            SELECT * FROM investment_listings ORDER BY score_total DESC
        """)
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_stats(db_path: str) -> dict:
    async with aiosqlite.connect(db_path) as db:
        total = (await (await db.execute("SELECT COUNT(*) FROM investment_listings")).fetchone())[0]
        above_75 = (await (await db.execute("SELECT COUNT(*) FROM investment_listings WHERE score_total>=75")).fetchone())[0]
        notified = (await (await db.execute("SELECT COUNT(*) FROM investment_listings WHERE notified=1")).fetchone())[0]
    return {"total": total, "above_75": above_75, "notified": notified}


async def get_healthcheck_stats(db_path: str, since_iso: str) -> dict:
    """Задача 2026-08-14 (Г12, docs/data_collection_audit.md) — тот же
    источник, что видит живой писатель (bot/jobs/scheduler.py, SQLite
    bot.db), вместо мёртвого снимка в Postgres `investment_listings`
    (мигрирован один раз migrate_sqlite_to_pg.py 2026-06-05, с тех пор
    не обновлялся — /admin/dashboard/data и sheets_sync.py читали именно
    его, показывая 2-месячную давность статистику как текущую).
    first_seen/last_seen хранятся ISO-строками (см. upsert_investment_
    listing) — сравнение и MAX() строкового ISO 8601 лексикографически
    совпадает с хронологическим порядком, отдельного парсинга не нужно
    для WHERE/ORDER BY, но возвращаем last_found как строку — вызывающая
    сторона сама решает, парсить ли в datetime."""
    async with aiosqlite.connect(db_path) as db:
        total = (await (await db.execute(
            "SELECT COUNT(*) FROM investment_listings")).fetchone())[0]
        today = (await (await db.execute(
            "SELECT COUNT(*) FROM investment_listings WHERE first_seen >= ?",
            (since_iso,))).fetchone())[0]
        top_score = (await (await db.execute(
            "SELECT MAX(score_total) FROM investment_listings")).fetchone())[0]
        last_found = (await (await db.execute(
            "SELECT MAX(first_seen) FROM investment_listings")).fetchone())[0]
    return {
        "total": total or 0, "today": today or 0,
        "top_score": top_score or 0, "last_found": last_found,
    }
