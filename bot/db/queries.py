from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from bot.core.dedup import _hash_distance


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Users ─────────────────────────────────────────────────────────────────────

async def upsert_user(db_path: str, user_id: int, username: str | None) -> None:
    now = _now()
    from datetime import timedelta
    default_end = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT INTO users(user_id, username, subscription_end, role, created_at, updated_at)
            VALUES (?, ?, ?, 1, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET username = excluded.username,
                                               updated_at = excluded.updated_at
            """,
            (user_id, username, default_end, now, now),
        )
        await db.commit()


async def get_user(db_path: str, user_id: int) -> dict[str, Any] | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
    if row is None:
        return None
    d = dict(row)
    if d.get("priorities"):
        try:
            d["priorities"] = json.loads(d["priorities"])
        except (json.JSONDecodeError, TypeError):
            d["priorities"] = []
    if d.get("rooms"):
        try:
            d["rooms"] = json.loads(d["rooms"])
        except (json.JSONDecodeError, TypeError):
            d["rooms"] = d["rooms"].split(",") if d["rooms"] else []
    return d


async def save_user_prefs(db_path: str, user_id: int, prefs: dict[str, Any]) -> None:
    now = _now()
    priorities = json.dumps(prefs.get("priorities", []), ensure_ascii=False)
    rooms = json.dumps(prefs.get("rooms", []), ensure_ascii=False)
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            UPDATE users
            SET deal_type     = ?,
                city          = ?,
                district      = ?,
                budget_min    = ?,
                budget_max    = ?,
                rooms         = ?,
                area_min      = ?,
                move_in       = ?,
                priorities    = ?,
                owner_only    = ?,
                property_type = ?,
                updated_at    = ?
            WHERE user_id = ?
            """,
            (
                prefs.get("deal_type"),
                prefs.get("city"),
                prefs.get("district"),
                prefs.get("budget_min"),
                prefs.get("budget_max"),
                rooms,
                prefs.get("area_min"),
                prefs.get("move_in"),
                priorities,
                prefs.get("owner_only"),       # 1, 0, or None
                prefs.get("property_type"),    # 'new', 'secondary', or None
                now,
                user_id,
            ),
        )
        await db.commit()


async def get_all_active_users(db_path: str) -> list[dict[str, Any]]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM users WHERE deal_type IS NOT NULL AND city IS NOT NULL"
        )
        rows = await cursor.fetchall()
    result = []
    for row in rows:
        d = dict(row)
        if d.get("priorities"):
            try:
                d["priorities"] = json.loads(d["priorities"])
            except (json.JSONDecodeError, TypeError):
                d["priorities"] = []
        if d.get("rooms"):
            try:
                d["rooms"] = json.loads(d["rooms"])
            except (json.JSONDecodeError, TypeError):
                d["rooms"] = d["rooms"].split(",") if d["rooms"] else []
        result.append(d)
    return result


# ── Listings ──────────────────────────────────────────────────────────────────

async def save_listing(db_path: str, listing: dict[str, Any]) -> None:
    sources = json.dumps(listing.get("sources", []), ensure_ascii=False)
    raw_photo_urls = listing.get("photo_urls", [])
    if isinstance(raw_photo_urls, str):
        photo_urls_json = raw_photo_urls  # already serialised
    else:
        photo_urls_json = json.dumps(raw_photo_urls or [], ensure_ascii=False)
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT OR IGNORE INTO listings
              (id, url, title, price, area, rooms, floor, floors_total,
               address, district, city, deal_type, phone, complex_name,
               photo_url, photo_urls, published_at, found_at, sources)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                listing.get("id"),
                listing.get("url"),
                listing.get("title"),
                listing.get("price"),
                listing.get("area"),
                listing.get("rooms"),
                listing.get("floor"),
                listing.get("floors_total"),
                listing.get("address"),
                listing.get("district"),
                listing.get("city"),
                listing.get("deal_type"),
                listing.get("phone"),
                listing.get("complex_name"),
                listing.get("photo_url"),
                photo_urls_json,
                listing.get("published_at"),
                _now(),
                sources,
            ),
        )
        await db.commit()


async def reset_user_data(db_path: str, user_id: int) -> None:
    """Cascade-delete all activity history and reset filter fields to NULL for a user."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("DELETE FROM user_listings WHERE user_id=?", (user_id,))
        await db.execute("DELETE FROM user_listing_notifications WHERE user_id=?", (user_id,))
        await db.execute("DELETE FROM favorites WHERE user_id=?", (user_id,))
        await db.execute("DELETE FROM saved_searches WHERE user_id=?", (user_id,))
        await db.execute("DELETE FROM blocked_listings WHERE user_id=?", (user_id,))
        await db.execute("DELETE FROM listing_views WHERE user_id=?", (user_id,))
        await db.execute("DELETE FROM ai_cache WHERE user_id=?", (user_id,))
        await db.execute(
            """
            UPDATE users SET
                deal_type=NULL, city=NULL, district=NULL,
                budget_min=NULL, budget_max=NULL, rooms=NULL,
                area_min=NULL, move_in=NULL, priorities=NULL,
                owner_only=NULL, property_type=NULL,
                location_lat=NULL, location_lon=NULL, radius_km=NULL,
                updated_at=?
            WHERE user_id=?
            """,
            (_now(), user_id),
        )
        await db.commit()


async def get_listing(db_path: str, listing_id: str) -> dict[str, Any] | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM listings WHERE id = ?", (listing_id,)
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    d = dict(row)
    if d.get("sources"):
        try:
            d["sources"] = json.loads(d["sources"])
        except (json.JSONDecodeError, TypeError):
            d["sources"] = []
    return d


async def is_notified(db_path: str, user_id: int, listing_id: str) -> bool:
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT 1 FROM user_listing_notifications WHERE user_id=? AND listing_id=?",
            (user_id, listing_id),
        )
        return await cursor.fetchone() is not None


async def mark_notified(db_path: str, user_id: int, listing_id: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT OR IGNORE INTO user_listing_notifications(user_id, listing_id, notified_at) VALUES (?,?,?)",
            (user_id, listing_id, _now()),
        )
        await db.commit()


# ── Favorites ─────────────────────────────────────────────────────────────────

async def add_favorite(db_path: str, user_id: int, listing_id: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT OR IGNORE INTO favorites(user_id, listing_id, saved_at) VALUES (?,?,?)",
            (user_id, listing_id, _now()),
        )
        await db.commit()


async def remove_favorite(db_path: str, user_id: int, listing_id: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "DELETE FROM favorites WHERE user_id=? AND listing_id=?",
            (user_id, listing_id),
        )
        await db.commit()


async def is_favorite(db_path: str, user_id: int, listing_id: str) -> bool:
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT 1 FROM favorites WHERE user_id=? AND listing_id=?",
            (user_id, listing_id),
        )
        return await cursor.fetchone() is not None


async def get_favorites(db_path: str, user_id: int) -> list[dict[str, Any]]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT l.* FROM favorites f
            JOIN listings l ON l.id = f.listing_id
            WHERE f.user_id = ?
            ORDER BY f.saved_at DESC
            """,
            (user_id,),
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def count_favorites(db_path: str, user_id: int) -> int:
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM favorites WHERE user_id=?", (user_id,)
        )
        row = await cursor.fetchone()
    return row[0] if row else 0


# ── Blocked listings ──────────────────────────────────────────────────────────

async def block_listing(db_path: str, user_id: int, listing_id: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT OR IGNORE INTO blocked_listings(user_id, listing_id, blocked_at) VALUES (?,?,?)",
            (user_id, listing_id, _now()),
        )
        await db.commit()


async def is_blocked(db_path: str, user_id: int, listing_id: str) -> bool:
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT 1 FROM blocked_listings WHERE user_id=? AND listing_id=?",
            (user_id, listing_id),
        )
        return await cursor.fetchone() is not None


# ── Saved searches ────────────────────────────────────────────────────────────

async def add_saved_search(db_path: str, user_id: int, listing_id: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO saved_searches(user_id, listing_id, created_at) VALUES (?,?,?)",
            (user_id, listing_id, _now()),
        )
        await db.commit()


async def is_following(db_path: str, user_id: int, listing_id: str) -> bool:
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT 1 FROM saved_searches WHERE user_id=? AND listing_id=?",
            (user_id, listing_id),
        )
        return await cursor.fetchone() is not None


# ── Listing views ─────────────────────────────────────────────────────────────

async def log_view(db_path: str, user_id: int, listing_id: str, action: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO listing_views(user_id, listing_id, action, ts) VALUES (?,?,?,?)",
            (user_id, listing_id, action, _now()),
        )
        await db.commit()


# ── AI cache ──────────────────────────────────────────────────────────────────

async def get_ai_explanation(
    db_path: str, listing_id: str, user_id: int
) -> str | None:
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT explanation FROM ai_cache WHERE listing_id=? AND user_id=?",
            (listing_id, user_id),
        )
        row = await cursor.fetchone()
    return row[0] if row else None


# ── Geo / location ────────────────────────────────────────────────────────────

async def save_user_location(
    db_path: str,
    user_id: int,
    lat: float | None,
    lon: float | None,
    radius_km: int | None,
) -> None:
    """Save or clear the user's geo filter (lat=None clears it)."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE users SET location_lat=?, location_lon=?, radius_km=? WHERE user_id=?",
            (lat, lon, radius_km, user_id),
        )
        await db.commit()


async def get_users_with_location(db_path: str) -> list[dict[str, Any]]:
    """Return users that have a geo filter set and an active subscription."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT * FROM users
            WHERE location_lat IS NOT NULL
              AND location_lon IS NOT NULL
              AND radius_km IS NOT NULL
              AND deal_type IS NOT NULL
              AND city IS NOT NULL
            """
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def save_listing_coords(db_path: str, listing_id: str, lat: float, lon: float) -> None:
    """Cache geocoded coordinates for a listing."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE listings SET lat=?, lon=? WHERE id=?",
            (lat, lon, listing_id),
        )
        await db.commit()


async def get_listing_coords(db_path: str, listing_id: str) -> tuple[float, float] | None:
    """Return cached (lat, lon) for a listing, or None if not yet geocoded."""
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT lat, lon FROM listings WHERE id=? AND lat IS NOT NULL AND lon IS NOT NULL",
            (listing_id,),
        )
        row = await cursor.fetchone()
    return (row[0], row[1]) if row else None


async def save_ai_explanation(
    db_path: str, listing_id: str, user_id: int, explanation: str
) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO ai_cache(listing_id, user_id, explanation, created_at)
            VALUES (?,?,?,?)
            """,
            (listing_id, user_id, explanation, _now()),
        )
        await db.commit()


# ── Photo hash deduplication ──────────────────────────────────────────────────

async def find_similar_photo_hash(
    db_path: str, photo_hash: str, threshold: int = 8
) -> str | None:
    """Return listing_id of an existing listing with similar photo hash, or None."""
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT id, photo_hash FROM listings WHERE photo_hash IS NOT NULL"
        )
        rows = await cursor.fetchall()
    for listing_id, existing_hash in rows:
        dist = _hash_distance(photo_hash, existing_hash)
        if dist is not None and dist <= threshold:
            return listing_id
    return None


# ── User pause ────────────────────────────────────────────────────────────────

async def set_user_paused(db_path: str, user_id: int, paused: bool) -> None:
    """Set or clear the is_paused flag for a user."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE users SET is_paused=? WHERE user_id=?",
            (1 if paused else 0, user_id),
        )
        await db.commit()


# ── Recent listings matching user filters ─────────────────────────────────────

async def get_recent_listings_for_user(
    db_path: str,
    city: str | None,
    deal_type: str | None,
    budget_max: int | None,
    n: int = 5,
) -> list[dict[str, Any]]:
    """Return the n most recent listings from the listings table matching user's filters."""
    conditions: list[str] = []
    params: list[Any] = []
    if city:
        conditions.append("l.city = ?")
        params.append(city)
    if deal_type:
        conditions.append("l.deal_type = ?")
        params.append(deal_type)
    if budget_max and budget_max > 0:
        conditions.append("l.price <= ?")
        params.append(budget_max)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(n)
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"""
            SELECT l.id, l.title, l.price, l.address, l.url, l.found_at
            FROM listings l
            {where}
            ORDER BY l.found_at DESC
            LIMIT ?
            """,
            params,
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


# ── Last sent listings ────────────────────────────────────────────────────────

async def get_last_sent_listings(
    db_path: str, user_id: int, n: int = 5
) -> list[dict[str, Any]]:
    """Return last n listings sent to this user (joins user_listing_notifications with listings)."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT l.id, l.title, l.price, l.address, l.url, n.notified_at
            FROM user_listing_notifications n
            JOIN listings l ON l.id = n.listing_id
            WHERE n.user_id = ?
            ORDER BY n.notified_at DESC
            LIMIT ?
            """,
            (user_id, n),
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


# ── Favorites paginated ───────────────────────────────────────────────────────

async def get_favorites_paginated(
    db_path: str, user_id: int, offset: int = 0, limit: int = 5
) -> list[dict[str, Any]]:
    """Return paginated favorites for a user."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT l.id, l.title, l.price, l.address, l.url
            FROM favorites f
            JOIN listings l ON l.id = f.listing_id
            WHERE f.user_id = ?
            ORDER BY f.saved_at DESC
            LIMIT ? OFFSET ?
            """,
            (user_id, limit, offset),
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


# ── Followed listings for price monitoring ────────────────────────────────────

async def get_followed_listings_all(db_path: str) -> list[dict[str, Any]]:
    """Return all saved_searches with listing url/title joined."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT ss.id, ss.user_id, ss.listing_id, ss.price_last_seen,
                   l.url, l.title
            FROM saved_searches ss
            JOIN listings l ON l.id = ss.listing_id
            """
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def update_follow_price(db_path: str, follow_id: int, price: int) -> None:
    """Update price_last_seen for a followed listing."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE saved_searches SET price_last_seen=? WHERE id=?",
            (price, follow_id),
        )
        await db.commit()
