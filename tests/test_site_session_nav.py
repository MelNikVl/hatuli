"""Header status exposes no user/tier data and never changes access rights."""
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates

from bot.core import site_auth
from bot.site_auth_routes import register_site_auth_routes


@pytest.mark.asyncio
@pytest.mark.parametrize("user", [None, {"user_id": 42, "full_access": True,
                                      "email": "private@example.invalid"}])
async def test_status_is_private_boolean_only_with_mocked_session(monkeypatch, user):
    async def deny(*args, **kwargs):
        raise AssertionError("No database connection in navigation tests")
    monkeypatch.setattr("asyncpg.connect", deny)
    monkeypatch.setattr("asyncpg.create_pool", deny)
    lookup = AsyncMock(return_value=user)
    monkeypatch.setattr(site_auth, "get_user_by_session", lookup)
    app = FastAPI()
    register_site_auth_routes(app, Jinja2Templates(directory=str(
        Path(__file__).resolve().parents[1] / "bot" / "templates")))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="https://hatuli.test",
                                 cookies={"site_session": "isolated_session"}) as client:
        response = await client.get("/api/auth/status")
    assert response.status_code == 200
    assert response.json() == {"authenticated": bool(user)}
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "noindex" in response.headers["x-robots-tag"]
    assert "set-cookie" not in response.headers
    lookup.assert_awaited_once_with("isolated_session")


@pytest.mark.asyncio
async def test_anonymous_status_performs_no_database_query(monkeypatch):
    deny = AsyncMock(side_effect=AssertionError("Anonymous status must not query PostgreSQL"))
    monkeypatch.setattr(site_auth, "fetchrow", deny)
    app = FastAPI()
    register_site_auth_routes(app, object())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="https://hatuli.test") as client:
        response = await client.get("/api/auth/status")
    assert response.json() == {"authenticated": False}
    deny.assert_not_awaited()
