"""TLS-terminating proxy login regressions; no real credentials or services."""
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates

from bot.core import site_auth
from bot.site_auth_routes import register_site_auth_routes


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("HATULI_PUBLIC_URL", "https://hatuli.test")
    async def deny(*args, **kwargs):
        raise AssertionError("No database/network in proxy tests")
    monkeypatch.setattr("asyncpg.connect", deny)
    monkeypatch.setattr("asyncpg.create_pool", deny)
    redeem = AsyncMock(return_value="isolated_opaque_session")
    monkeypatch.setattr(site_auth, "consume_login_token", redeem)
    application = FastAPI()
    register_site_auth_routes(application, Jinja2Templates(directory=str(
        Path(__file__).resolve().parents[1] / "bot" / "templates")))
    return application, redeem


HEADERS = {"Origin": "https://hatuli.test", "Sec-Fetch-Site": "same-origin",
           "X-Hatuli-Login": "telegram"}


@pytest.mark.asyncio
async def test_public_https_origin_works_over_internal_http_and_cookie_stays_secure(app):
    application, redeem = app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                                 base_url="http://hatuli.test") as client:
        response = await client.post("/api/auth/complete", headers=HEADERS,
                                     json={"token": "isolated_test_token"})
    assert response.status_code == 200
    assert response.json() == {"status": "verified", "redirect": "/map"}
    assert all(flag in response.headers["set-cookie"] for flag in
               ("Secure", "HttpOnly", "SameSite=lax", "Path=/"))
    assert response.headers["cache-control"] == "no-store"
    redeem.assert_awaited_once_with("isolated_test_token")


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [
    {"Origin": "http://hatuli.test"}, {"Origin": "null"},
    {"Origin": "https://hatuli.test:8443"}, {"Origin": "https://evil.test"},
    {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
    {"X-Hatuli-Login": ""},
    {"Origin": "https://evil.test", "X-Forwarded-Host": "evil.test", "X-Forwarded-Proto": "https"},
])
async def test_proxy_fix_preserves_origin_and_fetch_guards(app, extra):
    application, redeem = app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                                 base_url="http://hatuli.test") as client:
        response = await client.post("/api/auth/complete", headers={**HEADERS, **extra},
                                     json={"token": "isolated_test_token"})
    assert response.status_code == 403 and "set-cookie" not in response.headers
    redeem.assert_not_awaited()


@pytest.mark.asyncio
async def test_arbitrary_host_does_not_inherit_configured_public_origin(app):
    application, redeem = app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                                 base_url="http://other.test") as client:
        response = await client.post("/api/auth/complete", headers=HEADERS,
                                     json={"token": "isolated_test_token"})
    assert response.status_code == 403 and "set-cookie" not in response.headers
    redeem.assert_not_awaited()


@pytest.mark.asyncio
async def test_proxy_malformed_probe_cannot_redeem_or_create_session(app):
    application, redeem = app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                                 base_url="http://hatuli.test") as client:
        response = await client.post("/api/auth/complete", headers=HEADERS, json={"token": "x"})
    assert response.status_code == 400 and "set-cookie" not in response.headers
    redeem.assert_not_awaited()


@pytest.mark.asyncio
async def test_exact_configured_https_port_is_supported(app, monkeypatch):
    application, redeem = app
    monkeypatch.setenv("HATULI_PUBLIC_URL", "https://hatuli.test:8443")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application),
                                 base_url="http://hatuli.test:8443") as client:
        response = await client.post("/api/auth/complete", headers={**HEADERS,
            "Origin": "https://hatuli.test:8443"}, json={"token": "isolated_test_token"})
    assert response.status_code == 200 and "Secure" in response.headers["set-cookie"]
    redeem.assert_awaited_once()
