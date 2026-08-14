"""Регрессия для Фазы L1 продуктового трека «Локация» (docs/location_
product_design.md §7, задача 2026-08-14), коммит 2 — bot/core/
admin_alert.py::notify_admin(), вынесенный из bot/core/ai_text_analysis.py
при появлении второго вызывающего места (osm_mirrors_healthcheck.py),
чтобы не заводить вторую независимую копию (тот же класс риска, что уже
не раз ловился в проекте — finish_level/_CLASS_SCORE/геоцентроид ЖК).
Без БД, без реальной сети — httpx.AsyncClient подменяется фейком."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest


class _FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code


class _FakeAsyncClient:
    """Подмена httpx.AsyncClient — фиксирует POST-вызовы, не бьёт в сеть."""
    calls: list = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        _FakeAsyncClient.calls.append((url, kwargs))
        return _FakeResponse(200)


class _RaisingAsyncClient(_FakeAsyncClient):
    async def post(self, url, **kwargs):
        _FakeAsyncClient.calls.append((url, kwargs))
        raise RuntimeError("network down")


@pytest.fixture(autouse=True)
def _reset_calls():
    _FakeAsyncClient.calls = []
    yield
    _FakeAsyncClient.calls = []


@pytest.mark.asyncio
async def test_notify_admin_noop_without_env(monkeypatch):
    import bot.core.admin_alert as admin_alert

    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.delenv("ADMIN_TELEGRAM_ID", raising=False)
    monkeypatch.setattr(admin_alert.httpx, "AsyncClient", _FakeAsyncClient)

    await admin_alert.notify_admin("test message")
    assert _FakeAsyncClient.calls == []  # без токена/id — сеть не трогается вовсе


@pytest.mark.asyncio
async def test_notify_admin_posts_chat_id_and_text_when_configured(monkeypatch):
    import bot.core.admin_alert as admin_alert

    monkeypatch.setenv("BOT_TOKEN", "fake-token")
    monkeypatch.setenv("ADMIN_TELEGRAM_ID", "12345")
    monkeypatch.setattr(admin_alert.httpx, "AsyncClient", _FakeAsyncClient)

    await admin_alert.notify_admin("⚠️ test alert")

    assert len(_FakeAsyncClient.calls) == 1
    url, kwargs = _FakeAsyncClient.calls[0]
    assert "fake-token" in url
    assert kwargs["json"]["chat_id"] == "12345"
    assert kwargs["json"]["text"] == "⚠️ test alert"


@pytest.mark.asyncio
async def test_notify_admin_swallows_network_errors(monkeypatch):
    """Уведомление — побочный эффект, сбой сети не должен ронять вызывающий
    скрипт (тот же контракт, что был у исходного _notify_admin)."""
    import bot.core.admin_alert as admin_alert

    monkeypatch.setenv("BOT_TOKEN", "fake-token")
    monkeypatch.setenv("ADMIN_TELEGRAM_ID", "12345")
    monkeypatch.setattr(admin_alert.httpx, "AsyncClient", _RaisingAsyncClient)

    await admin_alert.notify_admin("should not raise")  # не должно бросить исключение
