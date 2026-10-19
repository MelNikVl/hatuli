"""Bot return links and browser exchange; no Telegram requests or live DB."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates

import service_site_bot as site_bot
from bot.core import site_auth
from bot.core.site_login_link import site_origin, telegram_return_url
from bot.site_auth_routes import register_site_auth_routes


@pytest.fixture(autouse=True)
def no_external_services(monkeypatch):
    async def reject(*args, **kwargs):
        raise AssertionError("Login tests must not connect to PostgreSQL or Telegram")
    monkeypatch.setattr("asyncpg.connect", reject)
    monkeypatch.setattr("asyncpg.create_pool", reject)
    monkeypatch.delenv("HATULI_PUBLIC_URL", raising=False)


def message(text="/start original_login_token", *, chat_type="private", user_id=42):
    return SimpleNamespace(text=text, chat=SimpleNamespace(type=chat_type),
        from_user=SimpleNamespace(id=user_id, username="visitor", full_name="Visitor"), answer=AsyncMock())


def stub_bot(monkeypatch, *, status=None, subscribed=True, return_token="independent_return_token"):
    get_status = AsyncMock(return_value=status if status is not None else {"status": "pending"})
    verify = AsyncMock(return_value=return_token)
    membership = AsyncMock(return_value=subscribed)
    execute = AsyncMock()
    not_subscribed = AsyncMock()
    monkeypatch.setattr(site_auth, "get_token_status", get_status)
    monkeypatch.setattr(site_auth, "verify_login_and_create_return_token", verify)
    monkeypatch.setattr(site_auth, "mark_login_not_subscribed", not_subscribed)
    monkeypatch.setattr(site_bot, "_check_subscribed", membership)
    monkeypatch.setattr("bot.db.pg.execute", execute)
    return SimpleNamespace(get_status=get_status, verify=verify, membership=membership,
                           execute=execute, not_subscribed=not_subscribed)


@pytest.mark.asyncio
@pytest.mark.parametrize("subscribed", [True, None])
async def test_bot_gives_separate_authenticated_link_after_confirmation(monkeypatch, subscribed):
    mocks = stub_bot(monkeypatch, subscribed=subscribed)
    msg = message()
    await site_bot.cmd_start(msg, object())
    mocks.verify.assert_awaited_once_with("original_login_token", 42)
    answer = msg.answer.await_args
    button = answer.kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.text == "Открыть Hatuli"
    url = urlsplit(button.url)
    assert (url.scheme, url.netloc, url.path, url.query) == ("https", "hatuli.ai-groundtruth.com", "/auth/telegram", "")
    assert parse_qs(url.fragment) == {"token": ["independent_return_token"]}
    assert "original_login_token" not in button.url
    assert "10 минут" in answer.args[0] and "один раз" in answer.args[0]
    assert "independent_return_token" not in answer.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_type", ["group", "supergroup", "channel"])
async def test_login_links_are_never_issued_in_group_chats(monkeypatch, chat_type):
    mocks = stub_bot(monkeypatch)
    msg = message(chat_type=chat_type)
    await site_bot.cmd_start(msg, object())
    mocks.get_status.assert_not_awaited()
    mocks.execute.assert_not_awaited()
    mocks.verify.assert_not_awaited()
    assert "reply_markup" not in msg.answer.await_args.kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [{"status": "expired"}, {"status": "consumed"},
    {"status": "verified", "telegram_id": 42}, {"status": "not_subscribed", "telegram_id": 99}])
async def test_bot_rejects_expired_repeated_or_other_telegram_owner(monkeypatch, status):
    mocks = stub_bot(monkeypatch, status=status)
    await site_bot.cmd_start(message(), object())
    mocks.membership.assert_not_awaited()
    mocks.execute.assert_not_awaited()
    mocks.verify.assert_not_awaited()


@pytest.mark.asyncio
async def test_not_subscribed_and_concurrently_unavailable_user_get_no_login_link(monkeypatch):
    mocks = stub_bot(monkeypatch, subscribed=False)
    msg = message()
    await site_bot.cmd_start(msg, object())
    mocks.not_subscribed.assert_awaited_once_with("original_login_token", 42)
    mocks.verify.assert_not_awaited()
    assert "reply_markup" not in msg.answer.await_args.kwargs
    mocks = stub_bot(monkeypatch, return_token=None)
    msg = message()
    await site_bot.cmd_start(msg, object())
    assert "доступ закрыт" in msg.answer.await_args.args[0]
    assert msg.answer.await_args.kwargs["reply_markup"].inline_keyboard[0][0].url.endswith("/cabinet")


@pytest.mark.asyncio
async def test_start_without_token_has_site_button_but_no_authentication(monkeypatch):
    mocks = stub_bot(monkeypatch)
    msg = message("/start")
    await site_bot.cmd_start(msg, object())
    assert msg.answer.await_args.kwargs["reply_markup"].inline_keyboard[0][0].url.endswith("/cabinet")
    mocks.verify.assert_not_awaited()
    mocks.execute.assert_not_awaited()


@pytest.mark.parametrize("origin", ["http://hatuli.test", "https://user:secret@hatuli.test", "https://hatuli.test/path",
    "https://hatuli.test?next=bad", "https://hatuli.test#bad", "//hatuli.test"])
def test_public_origin_rejects_credentials_insecure_and_redirect_config(monkeypatch, origin):
    monkeypatch.setenv("HATULI_PUBLIC_URL", origin)
    with pytest.raises(ValueError):
        site_origin()


def test_custom_https_origin_and_fragment_encoding(monkeypatch):
    monkeypatch.setenv("HATULI_PUBLIC_URL", "https://hatuli.test:8443/")
    url = urlsplit(telegram_return_url("safe_token_123456"))
    assert url.netloc == "hatuli.test:8443" and not url.query
    assert parse_qs(url.fragment)["token"] == ["safe_token_123456"]


@pytest.fixture
def exchange_app(monkeypatch):
    redeem = AsyncMock(return_value="opaque_session_never_in_url")
    monkeypatch.setattr(site_auth, "consume_login_token", redeem)
    app = FastAPI()
    templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "bot" / "templates"))
    register_site_auth_routes(app, templates)
    @app.get("/map")
    async def opened_site(request: Request):
        return {"authenticated": request.cookies.get("site_session") == "opaque_session_never_in_url",
                "admin_session": request.cookies.get("admin_session")}
    return app, redeem


HEADERS = {"Origin": "https://hatuli.test", "Sec-Fetch-Site": "same-origin", "X-Hatuli-Login": "telegram"}


@pytest.mark.asyncio
async def test_preview_get_does_not_consume_token(exchange_app):
    app, redeem = exchange_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://hatuli.test") as client:
        response = await client.get("/auth/telegram#token=safe_token_123456")
    assert response.status_code == 200 and "Входим в Hatuli" in response.text
    assert "safe_token_123456" not in response.text and "set-cookie" not in response.headers
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    redeem.assert_not_awaited()


@pytest.mark.asyncio
async def test_return_exchange_sets_cookie_and_opens_authenticated_site_preserving_admin(exchange_app):
    app, redeem = exchange_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://hatuli.test",
                                cookies={"admin_session": "existing_verified_admin"}) as client:
        response = await client.post("/api/auth/complete", json={"token": "safe_token_123456", "next": "https://evil.test"}, headers=HEADERS)
        assert response.json() == {"status": "verified", "redirect": "/map"}
        cookie = response.headers["set-cookie"]
        assert all(flag in cookie for flag in ("HttpOnly", "Secure", "SameSite=lax", "Path=/"))
        assert "opaque_session_never_in_url" not in response.text
        assert response.headers["cache-control"] == "no-store"
        assert (await client.get(response.json()["redirect"])).json() == {
            "authenticated": True, "admin_session": "existing_verified_admin"}
    redeem.assert_awaited_once_with("safe_token_123456")


@pytest.mark.asyncio
@pytest.mark.parametrize("token", [None, "", "x", 123, [], {}, "a" * 101, "x" * 16 + "?next=evil"])
async def test_malformed_tokens_reject_without_database_access(exchange_app, token):
    app, redeem = exchange_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://hatuli.test") as client:
        response = await client.post("/api/auth/complete", json={"token": token}, headers=HEADERS)
    assert response.status_code == 400 and "set-cookie" not in response.headers
    redeem.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [{"Origin": "https://evil.test"}, {"Origin": "http://hatuli.test"},
    {"Origin": "https://hatuli.test:8443"}, {"Sec-Fetch-Site": "cross-site"},
    {"Sec-Fetch-Site": "same-site"}, {"X-Hatuli-Login": ""}])
async def test_cross_site_exchange_rejected_without_database_access(exchange_app, extra):
    app, redeem = exchange_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://hatuli.test") as client:
        response = await client.post("/api/auth/complete", json={"token": "safe_token_123456"}, headers={**HEADERS, **extra})
    assert response.status_code == 403 and "set-cookie" not in response.headers
    redeem.assert_not_awaited()


@pytest.mark.asyncio
async def test_wrong_content_type_invalid_json_and_oversize_body(exchange_app):
    app, redeem = exchange_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://hatuli.test") as client:
        plain = await client.post("/api/auth/complete", content='{"token":"safe_token_123456"}', headers=HEADERS)
        malformed = await client.post("/api/auth/complete", content='{"token":', headers={**HEADERS, "Content-Type": "application/json"})
        oversize = await client.post("/api/auth/complete", json={"token": "a" * 2000}, headers=HEADERS)
    assert [plain.status_code, malformed.status_code, oversize.status_code] == [415, 400, 413]
    redeem.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_reused_or_blocked_tokens_cannot_set_cookie(exchange_app):
    app, redeem = exchange_app
    redeem.return_value = None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://hatuli.test") as client:
        response = await client.post("/api/auth/complete", json={"token": "safe_token_123456"}, headers=HEADERS)
    assert response.status_code == 401 and response.json()["status"] == "expired"
    assert "set-cookie" not in response.headers
