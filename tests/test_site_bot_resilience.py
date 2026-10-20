"""Login API failures stay unconfirmed; all Telegram/database calls are mocked."""
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import (
    TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError,
    TelegramRetryAfter, TelegramServerError,
)
from aiogram.methods import GetChatMember

import service_site_bot as site_bot
from bot.core import site_auth
from bot.db import pg


@pytest.fixture(autouse=True)
def isolated_services(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://placeholder:placeholder@127.0.0.1:1/placeholder")
    monkeypatch.delenv("HATULI_PUBLIC_URL", raising=False)
    async def denied(*args, **kwargs):
        raise AssertionError("Live Telegram/database requests are forbidden")
    monkeypatch.setattr("asyncpg.connect", denied)
    monkeypatch.setattr("asyncpg.create_pool", denied)
    for name in ("init_pool", "execute", "fetch", "fetchrow", "fetchval"):
        monkeypatch.setattr(pg, name, denied)


def login_stubs(monkeypatch):
    status = AsyncMock(return_value={"status": "pending", "telegram_id": None})
    execute = AsyncMock()
    verify = AsyncMock(return_value="synthetic_return_token")
    mark = AsyncMock()
    monkeypatch.setattr(site_auth, "get_token_status", status)
    monkeypatch.setattr(site_auth, "verify_login_and_create_return_token", verify)
    monkeypatch.setattr(site_auth, "mark_login_not_subscribed", mark)
    monkeypatch.setattr(pg, "execute", execute)
    message = SimpleNamespace(text="/start original_synthetic_token", chat=SimpleNamespace(type="private"),
        from_user=SimpleNamespace(id=424242, username="synthetic", full_name="Synthetic User"), answer=AsyncMock())
    return SimpleNamespace(status=status, execute=execute, verify=verify, mark=mark, message=message)


def api_error(kind, text="sensitive_error_424242_original_synthetic_token"):
    method = GetChatMember(chat_id="@synthetic_channel", user_id=424242)
    return kind(method=method, message=text, **({"retry_after": 7} if kind is TelegramRetryAfter else {}))


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    api_error(TelegramNetworkError), api_error(TelegramForbiddenError),
    api_error(TelegramRetryAfter), api_error(TelegramServerError),
    api_error(TelegramBadRequest), TimeoutError("sensitive_error_424242_original_synthetic_token"),
])
async def test_membership_api_failures_offer_retry_without_authentication_or_sensitive_logs(monkeypatch, caplog, error):
    stubs = login_stubs(monkeypatch)
    bot = SimpleNamespace(get_chat_member=AsyncMock(side_effect=error))
    with caplog.at_level(logging.WARNING, logger="site_bot"):
        await site_bot.cmd_start(stubs.message, bot)
    stubs.execute.assert_not_awaited()
    stubs.verify.assert_not_awaited()
    stubs.mark.assert_not_awaited()
    stubs.status.assert_awaited_once_with("original_synthetic_token")
    answer = stubs.message.answer.await_args
    assert all(text in answer.args[0] for text in ("исходную ссылку", "Start", "10 минут", "новую на сайте"))
    assert "Готово" not in answer.args[0]
    assert answer.kwargs["reply_markup"].inline_keyboard[0][0].url == "https://hatuli.ai-groundtruth.com/cabinet"
    assert type(error).__name__ in caplog.text
    assert "phase=membership_unavailable policy=retry" in caplog.text
    assert all(value not in caplog.text for value in ("424242", "sensitive_error", "original_synthetic_token"))


@pytest.mark.asyncio
@pytest.mark.parametrize("description", ["member list is inaccessible", "not enough rights"])
async def test_existing_inaccessible_membership_policy_still_allows_unknown_status_without_private_logs(monkeypatch, caplog, description):
    stubs = login_stubs(monkeypatch)
    bot = SimpleNamespace(get_chat_member=AsyncMock(side_effect=api_error(
        TelegramBadRequest, description + " sensitive_error_424242_original_synthetic_token")))
    with caplog.at_level(logging.WARNING, logger="site_bot"):
        await site_bot.cmd_start(stubs.message, bot)
    assert stubs.execute.await_args.args[-1] is None
    stubs.verify.assert_awaited_once_with("original_synthetic_token", 424242)
    stubs.mark.assert_not_awaited()
    assert "Готово" in stubs.message.answer.await_args.args[0]
    assert all(value not in caplog.text for value in ("424242", "sensitive_error", "original_synthetic_token"))


@pytest.mark.asyncio
async def test_confirmed_nonmember_remains_a_subscription_failure(monkeypatch, caplog):
    stubs = login_stubs(monkeypatch)
    bot = SimpleNamespace(get_chat_member=AsyncMock(return_value=SimpleNamespace(status="left")))
    with caplog.at_level(logging.INFO, logger="site_bot"):
        await site_bot.cmd_start(stubs.message, bot)
    assert stubs.execute.await_args.args[-1] is False
    stubs.mark.assert_awaited_once_with("original_synthetic_token", 424242)
    stubs.verify.assert_not_awaited()
    assert "подпишись" in stubs.message.answer.await_args.args[0]
    assert "phase=not_subscribed" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("status,phase", [
    (None, "token_not_found"), ({"status": "expired"}, "expired"),
    ({"status": "pending", "telegram_id": 999999}, "owner_mismatch"),
    ({"status": "verified", "telegram_id": 424242}, "token_unavailable"),
])
async def test_rejected_token_phases_are_safe_and_do_not_check_membership(monkeypatch, caplog, status, phase):
    stubs = login_stubs(monkeypatch)
    stubs.status.return_value = status
    bot = SimpleNamespace(get_chat_member=AsyncMock())
    with caplog.at_level(logging.INFO, logger="site_bot"):
        await site_bot.cmd_start(stubs.message, bot)
    assert f"phase={phase}" in caplog.text
    assert all(value not in caplog.text for value in ("424242", "999999", "original_synthetic_token"))
    bot.get_chat_member.assert_not_awaited()
    stubs.execute.assert_not_awaited()
    stubs.verify.assert_not_awaited()
    stubs.mark.assert_not_awaited()


@pytest.mark.asyncio
async def test_bare_start_explains_fresh_site_link_without_authentication(monkeypatch, caplog):
    stubs = login_stubs(monkeypatch)
    stubs.message.text = "/start"
    bot = SimpleNamespace(get_chat_member=AsyncMock())
    with caplog.at_level(logging.INFO, logger="site_bot"):
        await site_bot.cmd_start(stubs.message, bot)
    assert "phase=start_without_token" in caplog.text
    assert "перейди по новой ссылке и нажми Start" in stubs.message.answer.await_args.args[0]
    assert "не выполняет вход" in stubs.message.answer.await_args.args[0]
    bot.get_chat_member.assert_not_awaited()
    stubs.status.assert_not_awaited()
    stubs.execute.assert_not_awaited()
    stubs.verify.assert_not_awaited()


@pytest.mark.asyncio
async def test_issued_return_is_logged_only_as_a_phase_without_any_credentials(monkeypatch, caplog):
    stubs = login_stubs(monkeypatch)
    bot = SimpleNamespace(get_chat_member=AsyncMock(return_value=SimpleNamespace(status="member")))
    with caplog.at_level(logging.INFO, logger="site_bot"):
        await site_bot.cmd_start(stubs.message, bot)
    assert "phase=return_issued" in caplog.text
    assert all(value not in caplog.text for value in ("424242", "original_synthetic_token", "synthetic_return_token"))
    stubs.verify.assert_awaited_once_with("original_synthetic_token", 424242)


@pytest.mark.asyncio
async def test_startup_logs_revision_without_settings_write_or_real_polling(monkeypatch, caplog):
    from bot import git_info
    monkeypatch.setattr(site_bot, "SITE_BOT_TOKEN", "synthetic_placeholder_token")
    monkeypatch.setattr(site_bot, "DATABASE_URL", "postgresql://placeholder:placeholder@127.0.0.1:1/placeholder")
    initialize = AsyncMock()
    monkeypatch.setattr(pg, "init_pool", initialize)
    bot = object()
    monkeypatch.setattr(site_bot, "Bot", lambda token: bot)
    monkeypatch.setattr(git_info, "git_hash", lambda: "abc1234")
    polling = AsyncMock()
    monkeypatch.setattr(site_bot.dp, "start_polling", polling)
    def close_digest(coroutine):
        coroutine.close()
        return None
    monkeypatch.setattr(site_bot.asyncio, "create_task", close_digest)
    with caplog.at_level(logging.INFO, logger="site_bot"):
        await site_bot.main()
    assert "Site bot started (git=abc1234)" in caplog.text
    assert "synthetic_placeholder_token" not in caplog.text
    initialize.assert_awaited_once_with("postgresql://placeholder:placeholder@127.0.0.1:1/placeholder")
    polling.assert_awaited_once_with(bot)
