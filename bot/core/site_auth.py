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

from fastapi import Request

from bot.db.pg import execute, fetch, fetchrow

TOKEN_TTL_MIN = 10

_full_access_col_ready = False


async def get_user_tier(request: Request) -> str:
    """3 уровня доступа (задача "3 уровня доступа", 2026-08-07;
    переформулировано "общий доступ", 2026-08-12) — общая для admin_web.py
    (/, /admin, /listing/{id}) и terminal_extras.py (map-points и т.д.),
    раньше была задублирована в terminal_extras.py как локальная функция.

    admin — проверенная серверная admin_session (пароль в /admin/login, отдельная система от
    этой). subscriber — залогинен через Telegram И вручную выдан
    full_access администратором (/admin/site-users) — весь сайт открыт,
    кроме админки. public — аноним ИЛИ залогинен через Telegram, но
    full_access ещё не выдан (регистрация сама по себе доступ не даёт):
    видит только новостройки (market_type='primary') + тепловые карты +
    разделы главного меню (ЖК/застройщики/банки/новости) как каталог —
    но не карточки отдельных объявлений вторички."""
    from bot.core.admin_sessions import is_admin
    if is_admin(request):
        return "admin"
    session = request.cookies.get("site_session")
    if session:
        user = await get_user_by_session(session)
        if user and user.get("full_access"):
            return "subscriber"
    return "public"


async def create_login_token() -> str:
    token = secrets.token_urlsafe(24)
    await execute(
        "INSERT INTO login_tokens (token, status) VALUES ($1, 'pending')", token)
    return token


async def get_token_status(token: str) -> dict | None:
    if not token or len(token) > 100:
        return None
    row = await fetchrow(
        "SELECT token, telegram_id, status, created_at, consumed_at FROM login_tokens WHERE token = $1",
        token)
    if not row:
        return None
    age = datetime.now(timezone.utc) - row["created_at"] if row["created_at"] else timedelta.max
    if age > timedelta(minutes=TOKEN_TTL_MIN) or row.get("consumed_at") is not None:
        return {"status": "expired"}
    return dict(row)


async def create_session(user_id: int) -> str:
    session_id = secrets.token_urlsafe(32)
    row = await fetchrow(
        "INSERT INTO site_sessions (session_id, user_id) "
        "SELECT $1, user_id FROM users WHERE user_id=$2 AND COALESCE(is_blocked,0)=0 "
        "RETURNING session_id", session_id, user_id)
    if not row:
        raise ValueError("Site user is unavailable")
    return session_id


async def consume_login_token(token: str) -> str | None:
    """Atomically exchange a fresh verified token once, including concurrent polls."""
    if not token or len(token) > 100:
        return None
    session_id = secrets.token_urlsafe(32)
    row = await fetchrow("""
        WITH consumed AS (
            UPDATE login_tokens t SET consumed_at=now(), status='consumed'
            FROM users u WHERE t.token=$1 AND t.telegram_id=u.user_id
              AND t.status='verified' AND t.consumed_at IS NULL
              AND t.created_at > now() - $3::int * interval '1 minute'
              AND COALESCE(u.is_blocked,0)=0
            RETURNING t.telegram_id
        )
        INSERT INTO site_sessions (session_id,user_id)
        SELECT $2,telegram_id FROM consumed RETURNING session_id
    """, token, session_id, TOKEN_TTL_MIN)
    return row["session_id"] if row else None


async def get_user_by_session(session_id: str | None) -> dict | None:
    if not session_id:
        return None
    row = await fetchrow("""
        SELECT u.* FROM site_sessions s
        JOIN users u ON u.user_id = s.user_id
        WHERE s.session_id = $1 AND COALESCE(u.is_blocked,0)=0
          AND s.created_at > now() - interval '180 days'
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
               a.score_total, a.district, a.ceiling_height, dv.name AS developer_name
        FROM favorites f
        LEFT JOIN apartment_listings a ON a.id = f.listing_id
        LEFT JOIN complexes cx ON lower(trim(cx.name)) = lower(trim(a.complex_name))
        LEFT JOIN developers dv ON dv.id = cx.developer_id
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


# ── Избранные ЖК (не квартиры) — уведомления по любым изменениям в ЖК ──────

async def _ensure_complex_favorites_table() -> None:
    await execute("""
        CREATE TABLE IF NOT EXISTS complex_favorites (
            user_id BIGINT NOT NULL,
            complex_id INT NOT NULL,
            saved_at TIMESTAMPTZ DEFAULT now(),
            PRIMARY KEY (user_id, complex_id)
        )
    """)


async def list_favorite_complexes(user_id: int) -> list[dict]:
    await _ensure_complex_favorites_table()
    rows = await fetch("""
        SELECT cf.complex_id, cf.saved_at, c.name, c.district, c.housing_class,
               c.year_built, c.photo_url
        FROM complex_favorites cf
        LEFT JOIN complexes c ON c.id = cf.complex_id
        WHERE cf.user_id = $1
        ORDER BY cf.saved_at DESC
    """, user_id)
    out = []
    for r in rows:
        d = dict(r)
        if d.get("saved_at") is not None:
            d["saved_at"] = d["saved_at"].isoformat()
        out.append(d)
    return out


async def add_favorite_complex(user_id: int, complex_id: int) -> None:
    await _ensure_complex_favorites_table()
    await execute("""
        INSERT INTO complex_favorites (user_id, complex_id, saved_at) VALUES ($1, $2, now())
        ON CONFLICT (user_id, complex_id) DO NOTHING
    """, user_id, complex_id)


async def remove_favorite_complex(user_id: int, complex_id: int) -> None:
    await _ensure_complex_favorites_table()
    await execute(
        "DELETE FROM complex_favorites WHERE user_id = $1 AND complex_id = $2",
        user_id, complex_id)


async def is_favorite_complex_ids(user_id: int, complex_ids: list[int]) -> set[int]:
    if not complex_ids:
        return set()
    await _ensure_complex_favorites_table()
    rows = await fetch(
        "SELECT complex_id FROM complex_favorites WHERE user_id = $1 AND complex_id = ANY($2::int[])",
        user_id, complex_ids)
    return {r["complex_id"] for r in rows}


# ── Избранные зоны — уведомления по изменению цен на объекты внутри зоны ───

async def _ensure_zone_favorites_table() -> None:
    await execute("""
        CREATE TABLE IF NOT EXISTS zone_favorites (
            user_id BIGINT NOT NULL,
            zone_id INT NOT NULL REFERENCES priority_zones(id) ON DELETE CASCADE,
            saved_at TIMESTAMPTZ DEFAULT now(),
            PRIMARY KEY (user_id, zone_id)
        )
    """)


async def list_favorite_zones(user_id: int) -> list[dict]:
    await _ensure_zone_favorites_table()
    rows = await fetch("""
        SELECT zf.zone_id, zf.saved_at, z.name, z.color
        FROM zone_favorites zf
        LEFT JOIN priority_zones z ON z.id = zf.zone_id
        WHERE zf.user_id = $1
        ORDER BY zf.saved_at DESC
    """, user_id)
    out = []
    for r in rows:
        d = dict(r)
        if d.get("saved_at") is not None:
            d["saved_at"] = d["saved_at"].isoformat()
        out.append(d)
    return out


async def add_favorite_zone(user_id: int, zone_id: int) -> None:
    await _ensure_zone_favorites_table()
    await execute("""
        INSERT INTO zone_favorites (user_id, zone_id, saved_at) VALUES ($1, $2, now())
        ON CONFLICT (user_id, zone_id) DO NOTHING
    """, user_id, zone_id)


async def remove_favorite_zone(user_id: int, zone_id: int) -> None:
    await _ensure_zone_favorites_table()
    await execute(
        "DELETE FROM zone_favorites WHERE user_id = $1 AND zone_id = $2",
        user_id, zone_id)


async def is_favorite_zone_ids(user_id: int, zone_ids: list[int]) -> set[int]:
    if not zone_ids:
        return set()
    await _ensure_zone_favorites_table()
    rows = await fetch(
        "SELECT zone_id FROM zone_favorites WHERE user_id = $1 AND zone_id = ANY($2::int[])",
        user_id, zone_ids)
    return {r["zone_id"] for r in rows}


# ── Админ: управление пользователями сайта (отдельно от admin_users) ───────

async def _ensure_full_access_column() -> None:
    # Задача "общий доступ" (2026-08-12): раньше вход через Telegram сам по
    # себе давал tier="subscriber" (весь сайт кроме админки, см.
    # get_user_tier() в terminal_extras.py). Теперь регистрация даёт только
    # tier="public", как у анонима — расширенный доступ выдаёт администратор
    # вручную через /admin/site-users, этим флагом.
    await execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS full_access BOOLEAN DEFAULT FALSE")


async def list_site_users() -> list[dict]:
    await _ensure_full_access_column()
    rows = await fetch("""
        SELECT user_id, username, full_name, email, notify_frequency,
               channel_subscribed, is_blocked, full_access, created_at,
               (SELECT COUNT(*) FROM favorites f WHERE f.user_id = users.user_id) AS favorites_count
        FROM users
        ORDER BY created_at DESC NULLS LAST
    """)
    return [dict(r) for r in rows]


async def set_user_blocked(user_id: int, blocked: bool) -> None:
    await execute("UPDATE users SET is_blocked = $2 WHERE user_id = $1", user_id, 1 if blocked else 0)
    if blocked:
        await execute("DELETE FROM site_sessions WHERE user_id=$1", user_id)


async def set_user_full_access(user_id: int, full_access: bool) -> None:
    await _ensure_full_access_column()
    await execute("UPDATE users SET full_access = $2 WHERE user_id = $1", user_id, full_access)


async def delete_site_user(user_id: int) -> None:
    await execute("DELETE FROM favorites WHERE user_id = $1", user_id)
    await execute("DELETE FROM complex_favorites WHERE user_id = $1", user_id)
    await execute("DELETE FROM zone_favorites WHERE user_id = $1", user_id)
    await execute("DELETE FROM site_sessions WHERE user_id = $1", user_id)
    await execute("DELETE FROM users WHERE user_id = $1", user_id)
