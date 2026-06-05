"""
Compatibility DB layer.

BotDB provides subscription management, scheduler helpers, admin-panel queries,
and parse-error logging. This is the full database backend for the bot — it
supplements the aiogram-specific tables in bot/db/models.py by also managing
the legacy subscription/user-filter schema imported from the old krisha_bot.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

ASTANA_TZ = timezone(timedelta(hours=5))
ROLE_DAYS = {1: 1, 2: 7, 3: 30}


@dataclass(slots=True)
class UserSettings:
    user_id: int
    username: str | None
    role: int
    subscription_end: str | None
    city: str | None
    deal_type: str | None
    price_min: int | None
    price_max: int | None
    area_min: int | None
    area_max: int | None
    daily_report_hour: int | None
    is_blocked: int


class BotDB:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    async def init(self) -> None:
        """Delegate all table creation to the unified models.init_db."""
        from bot.db.models import init_db
        await init_db(self.db_path)

    async def log_event(self, event_type: str, description: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO events(type, description, created_at) VALUES (?, ?, ?)",
                (event_type, description, datetime.now(timezone.utc).isoformat()),
            )
            await db.commit()

    async def upsert_user(self, user_id: int, username: str | None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        default_end = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO users(user_id, username, role, subscription_end, created_at)
                VALUES (?, ?, 1, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET username = excluded.username
                """,
                (user_id, username, default_end, now),
            )
            await db.commit()

    async def set_user_filters(
        self,
        user_id: int,
        city: str,
        deal_type: str,
        price_min: int,
        price_max: int,
        area_min: int,
        area_max: int,
        daily_report_hour: int,
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE users
                SET city=?, deal_type=?, budget_min=?, budget_max=?, area_min=?, area_max=?, daily_report_hour=?
                WHERE user_id=?
                """,
                (city, deal_type, price_min, price_max, area_min, area_max, daily_report_hour, user_id),
            )
            await db.commit()

    async def get_user(self, user_id: int) -> UserSettings | None:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT user_id, username, role, subscription_end, city, deal_type,
                       budget_min as price_min, budget_max as price_max,
                       area_min, area_max, daily_report_hour, is_blocked
                FROM users WHERE user_id=?
                """,
                (user_id,),
            )
            row = await cursor.fetchone()
        if not row:
            return None
        return UserSettings(*row)

    async def get_active_users(self) -> list[UserSettings]:
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT user_id, username, role, subscription_end, city, deal_type,
                       budget_min as price_min, budget_max as price_max,
                       area_min, area_max, daily_report_hour, is_blocked
                FROM users
                WHERE is_blocked=0
                  AND city IS NOT NULL
                  AND deal_type IS NOT NULL
                  AND subscription_end IS NOT NULL
                  AND subscription_end > ?
                """,
                (now,),
            )
            rows = await cursor.fetchall()
        return [UserSettings(*r) for r in rows]

    async def get_expired_users(self) -> list[UserSettings]:
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT user_id, username, role, subscription_end, city, deal_type,
                       budget_min as price_min, budget_max as price_max,
                       area_min, area_max, daily_report_hour, is_blocked
                FROM users
                WHERE is_blocked=0 AND subscription_end IS NOT NULL AND subscription_end <= ?
                """,
                (now,),
            )
            rows = await cursor.fetchall()
        return [UserSettings(*r) for r in rows]

    async def is_user_notified_about_listing(self, user_id: int, listing_id: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT 1 FROM user_listings WHERE user_id=? AND listing_id=?",
                (user_id, listing_id),
            )
            row = await cursor.fetchone()
        return row is not None

    async def save_listing(self, listing: Any, city: str, deal_type: str) -> None:
        import json as _json
        area = _extract_area_from_title(listing.title)
        photo_url = getattr(listing, "photo_url", None)
        raw_photo_urls = getattr(listing, "photo_urls", [])
        photo_urls_json = _json.dumps(raw_photo_urls or [], ensure_ascii=False)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT OR IGNORE INTO listings
                    (id, url, title, price, area, address, city, deal_type,
                     photo_url, photo_urls, found_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    listing.id,
                    listing.url,
                    listing.title,
                    listing.price,
                    area,
                    listing.address,
                    city,
                    deal_type,
                    photo_url,
                    photo_urls_json,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            await db.commit()

    async def mark_user_listing_notified(self, user_id: int, listing_id: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO user_listings(user_id, listing_id, notified_at) VALUES (?, ?, ?)",
                (user_id, listing_id, datetime.now(timezone.utc).isoformat()),
            )
            await db.commit()

    async def grant_subscription(self, user_id: int, role: int) -> str:
        if role not in ROLE_DAYS:
            raise ValueError("Role must be 1, 2, or 3")
        await self.upsert_user(user_id, None)
        end_date = datetime.now(timezone.utc) + timedelta(days=ROLE_DAYS[role])
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE users SET role=?, subscription_end=? WHERE user_id=?",
                (role, end_date.isoformat(), user_id),
            )
            await db.commit()
        return end_date.isoformat()

    async def set_user_blocked(self, user_id: int, blocked: bool) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("UPDATE users SET is_blocked=? WHERE user_id=?", (1 if blocked else 0, user_id))
            await db.commit()

    async def get_recent_events(self, limit: int = 100) -> list[tuple]:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT id, type, description, created_at FROM events ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            return await cursor.fetchall()

    async def get_dashboard_stats(self) -> dict[str, Any]:
        from datetime import datetime, timedelta, timezone
        async with aiosqlite.connect(self.db_path) as db:
            now = datetime.now(timezone.utc)
            since_day = (now - timedelta(days=1)).isoformat()
            now_iso = now.isoformat()

            total_users = (await (await db.execute("SELECT COUNT(*) FROM users")).fetchone())[0]
            active_users = (
                await (
                    await db.execute(
                        "SELECT COUNT(*) FROM users WHERE is_blocked=0 AND subscription_end IS NOT NULL AND subscription_end > ?",
                        (now_iso,),
                    )
                ).fetchone()
            )[0]
            new_day = (
                await (await db.execute("SELECT COUNT(*) FROM users WHERE created_at >= ?", (since_day,))).fetchone()
            )[0]
            parsed_today = (
                await (await db.execute("SELECT COUNT(*) FROM listings WHERE found_at >= ?", (since_day,))).fetchone()
            )[0]
            last_parser = (await (await db.execute("SELECT MAX(found_at) FROM listings")).fetchone())[0]
            total_listings = (await (await db.execute("SELECT COUNT(*) FROM listings")).fetchone())[0]

            since_10min = (now - timedelta(minutes=10)).isoformat()
            req_total = (await (await db.execute("SELECT COUNT(*) FROM bot_requests")).fetchone())[0]
            req_10min = (
                await (
                    await db.execute("SELECT COUNT(*) FROM bot_requests WHERE ts >= ?", (since_10min,))
                ).fetchone()
            )[0]

            # Parse errors in last 24h
            errors_24h = (
                await (
                    await db.execute(
                        "SELECT COUNT(*) FROM parse_errors WHERE ts >= ?", (since_day,)
                    )
                ).fetchone()
            )[0]

            db_size_mb = round(os.path.getsize(self.db_path) / 1024 / 1024, 2) if os.path.exists(self.db_path) else 0

            # Parser health: OK if last parse within 15 min
            parser_ok: bool = False
            if last_parser:
                try:
                    from datetime import datetime, timezone
                    lp = datetime.fromisoformat(last_parser)
                    if lp.tzinfo is None:
                        lp = lp.replace(tzinfo=timezone.utc)
                    parser_ok = (now - lp).total_seconds() < 900
                except Exception:
                    pass

            return {
                "total_users": total_users,
            "active_users": active_users,
            "new_users_day": new_day,
            "parsed_today": parsed_today,
            "last_parser": last_parser,
            "total_listings": total_listings,
            "req_total": req_total,
            "req_10min": req_10min,
            "db_size_mb": db_size_mb,
            "parse_interval": "1–5 мин, случайный",
            "errors_24h": errors_24h,
            "parser_ok": parser_ok,
            "db_path": self.db_path,
        }

    async def get_users_admin(self) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT user_id, username, role, subscription_end,
                       city, deal_type,
                       budget_min AS price_min, budget_max AS price_max,
                       area_min, area_max,
                       district, owner_only, property_type,
                       daily_report_hour, is_blocked
                FROM users
                ORDER BY created_at DESC
                """
            )
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def delete_user_cascade(self, user_id: int) -> None:
        """Delete all data for a user across every table."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM user_listings WHERE user_id=?", (user_id,))
            await db.execute("DELETE FROM user_listing_notifications WHERE user_id=?", (user_id,))
            await db.execute("DELETE FROM favorites WHERE user_id=?", (user_id,))
            await db.execute("DELETE FROM saved_searches WHERE user_id=?", (user_id,))
            await db.execute("DELETE FROM blocked_listings WHERE user_id=?", (user_id,))
            await db.execute("DELETE FROM listing_views WHERE user_id=?", (user_id,))
            await db.execute("DELETE FROM ai_cache WHERE user_id=?", (user_id,))
            await db.execute("DELETE FROM bot_requests WHERE user_id=?", (user_id,))
            await db.execute("DELETE FROM users WHERE user_id=?", (user_id,))
            await db.commit()

    async def get_user_daily_listings(
        self, user_id: int, day_start_utc: datetime, day_end_utc: datetime
    ) -> list[tuple]:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT l.address, l.price, l.area, l.url
                FROM user_listings ul
                JOIN listings l ON l.id = ul.listing_id
                WHERE ul.user_id=? AND ul.notified_at >= ? AND ul.notified_at < ?
                ORDER BY ul.notified_at DESC
                """,
                (user_id, day_start_utc.isoformat(), day_end_utc.isoformat()),
            )
            return await cursor.fetchall()

    async def has_daily_report_event(self, user_id: int, day_key: str) -> bool:
        marker = f"user:{user_id}|date:{day_key}"
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT 1 FROM events WHERE type='daily_report' AND description LIKE ? LIMIT 1",
                (f"%{marker}%",),
            )
            return await cursor.fetchone() is not None

    async def log_bot_request(self, user_id: int | None) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO bot_requests(user_id, ts) VALUES (?, ?)",
                (user_id, datetime.now(timezone.utc).isoformat()),
            )
            await db.commit()

    async def log_parse_error(self, error_type: str, message: str, url: str | None = None) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO parse_errors(ts, error_type, message, url) VALUES (?, ?, ?, ?)",
                (datetime.now(timezone.utc).isoformat(), error_type, message, url or ""),
            )
            await db.commit()

    async def get_parse_errors(self, limit: int = 50) -> list[tuple]:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT id, ts, error_type, message, url FROM parse_errors ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            return await cursor.fetchall()

    async def clear_parse_errors(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM parse_errors")
            await db.commit()

    async def get_per_user_stats(self) -> list[dict[str, Any]]:
        """Per-user stats including owner_only and property_type filters."""
        from datetime import datetime, timedelta, timezone
        since_day = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT
                    u.user_id,
                    u.username,
                    u.city,
                    u.deal_type,
                    u.budget_max,
                    u.owner_only,
                    u.property_type,
                    (SELECT COUNT(*) FROM user_listings ul WHERE ul.user_id = u.user_id) AS listings_total,
                    (SELECT COUNT(*) FROM user_listings ul
                     WHERE ul.user_id = u.user_id AND ul.notified_at >= ?) AS listings_24h,
                    u.updated_at AS last_active
                FROM users u
                ORDER BY listings_total DESC
                """,
                (since_day,),
            )
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_last_listings(self, n: int = 20) -> list[tuple]:
        """Last N listings found by parser, ordered by found_at DESC."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT id, title, price, address, city, deal_type, found_at
                FROM listings
                ORDER BY found_at DESC
                LIMIT ?
                """,
                (n,),
            )
            return await cursor.fetchall()

    async def get_parser_cycle_info(self) -> dict[str, Any]:
        """Latest parser event info: last_run, listings_found."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT description, created_at
                FROM events
                WHERE type = 'parser'
                ORDER BY id DESC
                LIMIT 1
                """
            )
            row = await cursor.fetchone()
        if not row:
            return {"last_run": None, "listings_found": None}

        description, created_at = row
        # Parse listings count from description like "city=... listings=N"
        import re
        match = re.search(r"listings=(\d+)", description or "")
        listings_found = int(match.group(1)) if match else None
        return {"last_run": created_at, "listings_found": listings_found, "description": description}


def _extract_area_from_title(title: str) -> float | None:
    match = re.search(r"(\d+[\.,]?\d*)\s*м²", title.lower())
    if not match:
        return None
    value = match.group(1).replace(",", ".")
    try:
        return float(value)
    except ValueError:
        logger.warning("Failed to parse area from title: %s", title)
        return None