"""
Личный кабинет посетителя сайта: вход только через Telegram (без паролей).

Поток:
  1. Сайт: POST /api/auth/start -> создаёт одноразовый token (login_tokens),
     отдаёт deep-link https://t.me/<SITE_BOT_USERNAME>?start=<token>.
  2. Пользователь открывает Telegram, жмёт Start у бота.
  3. Бот (service_site_bot.py) проверяет подписку на HATULI_CHANNEL через
     get_chat_member, апсертит users по telegram_id, помечает token verified
     (или not_subscribed, если подписки нет).
  4. Сайт: GET /api/auth/poll?token=... — как только token verified, создаёт
     site_sessions и ставит cookie.

Отдельно от admin_users (это операторы админки, другая система авторизации).
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone, timedelta

from bot.db.pg import execute, fetch, fetchrow

TOKEN_TTL_MIN = 10


async def create_login_token() -> str:
    token = secrets.token_urlsafe(24)
    await execute(
        "INSERT INTO login_tokens (token, status) VALUES ($1, 'pending')", token)
    return token


async def get_token_status(token: str) -> dict | None:
    row = await fetchrow(
        "SELECT token, telegram_id, status, created_at FROM login_tokens WHERE token = $1",
        token)
    if not row:
        return None
    age = datetime.now(timezone.utc) - row["created_at"]
    if age > timedelta(minutes=TOKEN_TTL_MIN) and row["status"] == "pending":
        return {"status": "expired"}
    return dict(row)


async def create_session(user_id: int) -> str:
    session_id = secrets.token_urlsafe(32)
    await execute(
        "INSERT INTO site_sessions (session_id, user_id) VALUES ($1, $2)",
        session_id, user_id)
    return session_id


async def get_user_by_session(session_id: str | None) -> dict | None:
    if not session_id:
        return None
    row = await fetchrow("""
        SELECT u.* FROM site_sessions s
        JOIN users u ON u.user_id = s.user_id
        WHERE s.session_id = $1
    """, session_id)
    return dict(row) if row else None


async def destroy_session(session_id: str | None) -> None:
    if session_id:
        await execute("DELETE FROM site_sessions WHERE session_id = $1", session_id)


async def update_profile(user_id: int, full_name: str | None, email: str | None,
                         notify_frequency: str | None) -> None:
    await execute("""
        UPDATE users SET
            full_name = COALESCE($2, full_name),
            email = COALESCE($3, email),
            notify_frequency = COALESCE($4, notify_frequency),
            updated_at = now()
        WHERE user_id = $1
    """, user_id, full_name, email, notify_frequency)


async def list_favorites(user_id: int) -> list[dict]:
    import json
    rows = await fetch("""
        SELECT f.listing_id, f.saved_at, a.price, a.rooms, a.area, a.address,
               a.complex_name, a.url, a.photos, a.is_active, a.floor, a.floors_total,
               a.score_total, a.district
        FROM favorites f
        LEFT JOIN apartment_listings a ON a.id = f.listing_id
        WHERE f.user_id = $1
        ORDER BY f.saved_at DESC
    """, user_id)
    out = []
    for r in rows:
        d = dict(r)
        photos = d.get("photos")
        if isinstance(photos, str):
            try:
                photos = json.loads(photos)
            except ValueError:
                photos = []
        d["first_photo"] = (photos or [None])[0]
        if d.get("saved_at") is not None:
            d["saved_at"] = d["saved_at"].isoformat()
        out.append(d)
    return out


async def add_favorite(user_id: int, listing_id: str) -> None:
    await execute("""
        INSERT INTO favorites (user_id, listing_id, saved_at) VALUES ($1, $2, now())
        ON CONFLICT (user_id, listing_id) DO NOTHING
    """, user_id, listing_id)


async def remove_favorite(user_id: int, listing_id: str) -> None:
    await execute(
        "DELETE FROM favorites WHERE user_id = $1 AND listing_id = $2",
        user_id, listing_id)


async def is_favorite_ids(user_id: int, listing_ids: list[str]) -> set[str]:
    if not listing_ids:
        return set()
    rows = await fetch(
        "SELECT listing_id FROM favorites WHERE user_id = $1 AND listing_id = ANY($2::text[])",
        user_id, listing_ids)
    return {r["listing_id"] for r in rows}


# ── Админ: управление пользователями сайта (отдельно от admin_users) ───────

async def list_site_users() -> list[dict]:
    rows = await fetch("""
        SELECT user_id, username, full_name, email, notify_frequency,
               channel_subscribed, is_blocked, created_at,
               (SELECT COUNT(*) FROM favorites f WHERE f.user_id = users.user_id) AS favorites_count
        FROM users
        ORDER BY created_at DESC NULLS LAST
    """)
    return [dict(r) for r in rows]


async def set_user_blocked(user_id: int, blocked: bool) -> None:
    await execute("UPDATE users SET is_blocked = $2 WHERE user_id = $1", user_id, 1 if blocked else 0)


async def delete_site_user(user_id: int) -> None:
    await execute("DELETE FROM favorites WHERE user_id = $1", user_id)
    await execute("DELETE FROM site_sessions WHERE user_id = $1", user_id)
    await execute("DELETE FROM users WHERE user_id = $1", user_id)
