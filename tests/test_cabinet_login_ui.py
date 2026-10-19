"""Cabinet header follows its verified user without changing access tiers."""
from unittest.mock import AsyncMock
from pathlib import Path
import shutil
import subprocess

import httpx
import pytest

from bot.admin_web import create_admin_app
from bot.core import site_auth
from tests.test_public_comparison_routes import NoPrivateDB


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://placeholder:placeholder@127.0.0.1:1/placeholder")
    async def denied(*args, **kwargs):
        raise AssertionError("Cabinet UI tests must not access a database")
    monkeypatch.setattr("asyncpg.connect", denied)
    monkeypatch.setattr("asyncpg.create_pool", denied)
    monkeypatch.setattr("bot.db.pg.init_pool", denied)
    user = {"user_id": 42, "username": "visitor", "full_name": "Visitor", "email": None,
            "notify_frequency": "off", "full_access": False}
    async def get_user(session):
        return user if session == "placeholder_session" else None
    lookup = AsyncMock(side_effect=get_user)
    tier = AsyncMock(return_value="public")
    favorites = AsyncMock(return_value=[])
    complexes = AsyncMock(return_value=[])
    monkeypatch.setattr(site_auth, "get_user_by_session", lookup)
    monkeypatch.setattr(site_auth, "get_user_tier", tier)
    monkeypatch.setattr(site_auth, "list_favorites", favorites)
    monkeypatch.setattr(site_auth, "list_favorite_complexes", complexes)
    return create_admin_app(NoPrivateDB(), "isolated-cabinet-secret", "test"), lookup, tier, favorites, complexes


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/cabinet", "/favorites"])
async def test_authenticated_header_and_cache_follow_verified_user_without_private_access(app, path):
    application, lookup, tier, favorites, complexes = app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="https://test",
                                cookies={"site_session": "placeholder_session", "admin_auth": "1"}) as client:
        response = await client.get(path)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert 'class="pub-user-chip">Кабинет' in response.text
    assert 'class="pub-login-btn"' not in response.text
    assert 'class="admin-panel-link"' not in response.text
    assert 'id="login-btn"' not in response.text
    lookup.assert_awaited_once_with("placeholder_session")
    tier.assert_awaited_once()
    favorites.assert_not_awaited()  # Registration does not grant secondary listing access.
    complexes.assert_awaited_once_with(42)


@pytest.mark.asyncio
async def test_anonymous_cabinet_has_safe_client_and_can_start_login(app):
    application, lookup, tier, favorites, complexes = app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="https://test") as client:
        response = await client.get("/cabinet")
    assert response.status_code == 200
    assert 'class="pub-login-btn"' in response.text
    assert '/static/hatuli-cabinet-login.js' in response.text
    assert 'role="status" aria-live="polite"' in response.text
    assert 'setInterval' not in response.text
    assert response.headers["cache-control"] == "no-store"
    lookup.assert_awaited_once_with(None)
    tier.assert_not_awaited()
    favorites.assert_not_awaited()
    complexes.assert_not_awaited()


@pytest.mark.asyncio
async def test_unrelated_public_page_does_not_gain_a_global_user_lookup(app):
    application, lookup, *_ = app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="https://test") as client:
        response = await client.get("/how-it-works")
    assert response.status_code == 200
    lookup.assert_not_awaited()


@pytest.mark.parametrize("filename", ["test_cabinet_login_client.cjs", "test_telegram_login_client.cjs"])
def test_isolated_browser_regressions_run_from_pytest(filename):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the isolated login client regression")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([node, "--test", "--test-reporter=tap", str(root / "tests" / filename)],
                            cwd=root, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
