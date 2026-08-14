"""Регрессия для Фазы L1 продуктового трека «Локация» (docs/location_
product_design.md §7, задача 2026-08-14), коммит 2 —
bot/score_layers/osm.py::check_mirrors() и osm_mirrors_healthcheck.py.
Без реальной сети — httpx.AsyncClient подменяется фейком; без БД
(check_mirrors()/notify_admin() её не трогают)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest


class _FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code


class _MixedAsyncClient:
    """2 живых (200), 1 HTTP-ошибка (406), 1 сетевой сбой (raise) — тот же
    состав, что реально наблюдался в проде (osm.py докстринг: "часто жив
    только 1 из 4"), здесь детерминированно через порядок OVERPASS_MIRRORS."""
    STATUS_BY_INDEX = [200, 406, 200, None]  # None -> исключение
    calls: list = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        idx = len(_MixedAsyncClient.calls)
        _MixedAsyncClient.calls.append(url)
        status = _MixedAsyncClient.STATUS_BY_INDEX[idx % len(_MixedAsyncClient.STATUS_BY_INDEX)]
        if status is None:
            raise RuntimeError("connection refused")
        return _FakeResponse(status)


class _AllAliveAsyncClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        return _FakeResponse(200)


class _AllDeadAsyncClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        return _FakeResponse(500)


@pytest.fixture(autouse=True)
def _reset_calls():
    _MixedAsyncClient.calls = []
    yield


@pytest.mark.asyncio
async def test_check_mirrors_reports_each_mirror_independently(monkeypatch):
    import bot.score_layers.osm as osm

    monkeypatch.setattr(osm.httpx, "AsyncClient", _MixedAsyncClient)
    result = await osm.check_mirrors()

    assert len(result) == len(osm.OVERPASS_MIRRORS)
    alive = [v for v in result.values() if v]
    dead = [v for v in result.values() if not v]
    assert len(alive) == 2  # индексы 0,2 -> 200
    assert len(dead) == 2   # индексы 1 (406), 3 (raise)


@pytest.mark.asyncio
async def test_osm_healthcheck_alerts_when_below_threshold(monkeypatch):
    # run_healthcheck() делает `from bot.score_layers.osm import check_mirrors`
    # и `from bot.core.admin_alert import notify_admin` ВНУТРИ функции
    # (отложенный импорт, тот же паттерн, что everywhere в этом проекте) —
    # патчим источники, не osm_mirrors_healthcheck (там этих имён нет на
    # уровне модуля).
    import osm_mirrors_healthcheck as healthcheck
    import bot.score_layers.osm as osm
    import bot.core.admin_alert as admin_alert

    async def _fake_check_mirrors():
        return {"m1": True, "m2": False, "m3": False, "m4": False}

    called = {}

    async def _fake_notify(text):
        called["text"] = text

    monkeypatch.setattr(osm, "check_mirrors", _fake_check_mirrors)
    monkeypatch.setattr(admin_alert, "notify_admin", _fake_notify)

    result = await healthcheck.run_healthcheck()
    assert result["alive"] == ["m1"]
    assert "text" in called
    assert "1/4" in called["text"]


@pytest.mark.asyncio
async def test_osm_healthcheck_no_alert_when_at_threshold(monkeypatch):
    import osm_mirrors_healthcheck as healthcheck
    import bot.score_layers.osm as osm
    import bot.core.admin_alert as admin_alert

    async def _fake_check_mirrors():
        return {"m1": True, "m2": True, "m3": False, "m4": False}

    called = {"n": 0}

    async def _fake_notify(text):
        called["n"] += 1

    monkeypatch.setattr(osm, "check_mirrors", _fake_check_mirrors)
    monkeypatch.setattr(admin_alert, "notify_admin", _fake_notify)

    result = await healthcheck.run_healthcheck()
    assert len(result["alive"]) == 2  # ровно ALIVE_THRESHOLD -> НЕ < порога, алерта нет
    assert called["n"] == 0
