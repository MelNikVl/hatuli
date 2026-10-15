"""Opaque PostgreSQL admin sessions shared by all web routers."""
from __future__ import annotations

import hashlib
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Request

from bot.db.pg import execute, fetchrow, fetchval

COOKIE_NAME = "admin_session"
SESSION_TTL_SECONDS = 12 * 3600
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}\Z")
log = logging.getLogger(__name__)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


async def create_admin_session(
    user_id: int, *, password_hash: str | None = None,
    ttl_seconds: int = SESSION_TTL_SECONDS,
) -> str:
    """Issue for an existing user; login passes the hash it actually verified.

    Comparing credentials in the INSERT prevents issuing a new session with
    an old password while another request changes that password.
    """
    if not 0 < ttl_seconds <= SESSION_TTL_SECONDS:
        raise ValueError("Invalid admin session lifetime")
    token = secrets.token_urlsafe(32)
    inserted = await fetchval("""
        INSERT INTO admin_sessions (token_hash, user_id, credential_hash, csrf_token, expires_at)
        SELECT $1, u.id, u.password_hash, $3, now() + $4::int * interval '1 second'
        FROM admin_users u WHERE u.id=$2 AND ($5::text IS NULL OR u.password_hash=$5)
        RETURNING user_id
    """, _token_hash(token), user_id, secrets.token_urlsafe(32), ttl_seconds, password_hash)
    if inserted is None:
        raise ValueError("Admin user or verified credentials changed")
    return token


async def load_admin_session(token: str | None) -> dict | None:
    if not token or not _TOKEN_RE.fullmatch(token):
        return None
    row = await fetchrow("""
        SELECT u.id, u.username, s.csrf_token, s.expires_at
        FROM admin_sessions s JOIN admin_users u ON u.id=s.user_id
        WHERE s.token_hash=$1 AND s.expires_at > now()
          AND s.credential_hash=u.password_hash
    """, _token_hash(token))
    return dict(row) if row else None


async def destroy_admin_session(token: str | None) -> None:
    if token and _TOKEN_RE.fullmatch(token):
        await execute("DELETE FROM admin_sessions WHERE token_hash=$1", _token_hash(token))


class AdminSessionMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            state = scope.setdefault("state", {})
            state["admin_user"] = None
            state["admin_csrf"] = ""
            request = Request(scope)
            try:
                user = await load_admin_session(request.cookies.get(COOKIE_NAME))
            except Exception:
                # Database failure must not accidentally grant operator access.
                log.warning("Admin session could not be verified", exc_info=True)
                user = None
            if user:
                state["admin_user"] = {"id": user["id"], "username": user["username"]}
                state["admin_csrf"] = user["csrf_token"]
        await self.app(scope, receive, send)


def is_admin(request: Request) -> bool:
    user = getattr(request.state, "admin_user", None)
    return bool(isinstance(user, dict) and user.get("id") and user.get("username"))


def admin_username(request: Request) -> str | None:
    return request.state.admin_user["username"] if is_admin(request) else None


def admin_csrf_token(request: Request) -> str:
    return getattr(request.state, "admin_csrf", "") if is_admin(request) else ""


def validate_admin_csrf(request: Request, token: str | None) -> bool:
    expected = admin_csrf_token(request)
    return bool(expected and isinstance(token, str) and token.isascii()
                and secrets.compare_digest(expected, token))


def set_admin_session_cookie(response, request: Request, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME, token, max_age=SESSION_TTL_SECONDS,
        expires=datetime.now(timezone.utc) + timedelta(seconds=SESSION_TTL_SECONDS),
        httponly=True, secure=request.url.scheme == "https", samesite="lax", path="/",
    )
    response.delete_cookie("admin_auth", path="/")
    response.delete_cookie("admin_user", path="/")


def clear_admin_session_cookie(response) -> None:
    for name in (COOKIE_NAME, "admin_auth", "admin_user"):
        response.delete_cookie(name, path="/")
