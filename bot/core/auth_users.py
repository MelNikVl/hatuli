"""
Пользователи админки: хранение в Postgres (admin_users), пароли — PBKDF2
(stdlib, без внешних зависимостей типа bcrypt/passlib).

Формат хранения: "<salt_hex>$<hash_hex>".
"""
from __future__ import annotations

import hashlib
import secrets

from bot.db.pg import execute, fetch, fetchrow

_ITERATIONS = 200_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), _ITERATIONS
    ).hex()
    return f"{salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, digest = stored.split("$", 1)
    except ValueError:
        return False
    check = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), _ITERATIONS
    ).hex()
    return secrets.compare_digest(check, digest)


async def ensure_seeded(default_admin_password: str) -> None:
    """Если таблица пуста (первый запуск после миграции) — создаёт 'admin'
    с паролем из ADMIN_PASSWORD (.env), чтобы не потерять доступ при переходе
    со старой однопользовательской схемы на admin_users."""
    rows = await fetch("SELECT 1 FROM admin_users LIMIT 1")
    if rows:
        return
    await execute(
        "INSERT INTO admin_users (username, password_hash) VALUES ($1, $2) "
        "ON CONFLICT (username) DO NOTHING",
        "admin", hash_password(default_admin_password),
    )


async def get_user(username: str):
    return await fetchrow("SELECT * FROM admin_users WHERE username = $1", username)


async def list_users():
    return await fetch("SELECT id, username, created_at FROM admin_users ORDER BY id")


async def create_user(username: str, password: str) -> bool:
    """True, если создан; False, если username уже занят."""
    existing = await get_user(username)
    if existing:
        return False
    await execute(
        "INSERT INTO admin_users (username, password_hash) VALUES ($1, $2)",
        username, hash_password(password),
    )
    return True


async def set_password(user_id: int, new_password: str) -> None:
    await execute(
        "UPDATE admin_users SET password_hash = $2 WHERE id = $1",
        user_id, hash_password(new_password),
    )
