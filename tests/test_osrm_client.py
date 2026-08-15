"""Регрессия для Фазы L3 (walkability, задача 2026-08-15, миграция 075) —
bot/core/osrm_client.py. Без реальной сети: клиент подменяется фейком
(тот же паттерн, что tests/test_osm_healthcheck.py), без БД."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _FakeClient:
    """Подмена httpx.AsyncClient: отдаёт заданный payload, запоминает URL."""
    def __init__(self, payload, status_code=200, raises=False):
        self._payload = payload
        self._status = status_code
        self._raises = raises
        self.urls: list[str] = []

    async def get(self, url):
        self.urls.append(url)
        if self._raises:
            raise RuntimeError("connection refused")
        return _FakeResponse(self._payload, self._status)


_ORIGIN = (51.13, 71.43)  # (lat, lon), центр Астаны
_CANDIDATES = [
    {"name": "POI ближний по прямой", "lat": 51.131, "lon": 71.431},
    {"name": "POI дальний по прямой", "lat": 51.135, "lon": 71.435},
]


@pytest.mark.asyncio
async def test_walking_table_enriches_candidates():
    from bot.core.osrm_client import walking_table
    payload = {
        "code": "Ok",
        # [origin->origin, origin->c1, origin->c2]
        "distances": [[0.0, 812.5, 430.0]],
        "durations": [[0.0, 610.0, 330.0]],
    }
    client = _FakeClient(payload)
    out = await walking_table(client, _ORIGIN, [dict(c) for c in _CANDIDATES])

    assert out is not None
    assert out[0]["walking_distance_m"] == 812.5
    assert out[0]["walking_duration_s"] == 610.0
    assert out[0]["no_route_reason"] is None
    assert out[1]["walking_distance_m"] == 430.0
    # URL: lon,lat порядок OSRM, origin первым, sources=0
    url = client.urls[0]
    assert "/table/v1/foot/71.43,51.13;71.431,51.131;71.435,51.135" in url
    assert "sources=0" in url


@pytest.mark.asyncio
async def test_walking_table_null_distance_marks_no_route():
    from bot.core.osrm_client import walking_table
    payload = {"code": "Ok",
               "distances": [[0.0, None, 430.0]],
               "durations": [[0.0, None, 330.0]]}
    client = _FakeClient(payload)
    out = await walking_table(client, _ORIGIN, [dict(c) for c in _CANDIDATES])

    assert out[0]["walking_distance_m"] is None
    assert out[0]["no_route_reason"] == "no_route"
    assert out[1]["walking_distance_m"] == 430.0


@pytest.mark.asyncio
async def test_walking_table_no_segment_marks_no_snap():
    from bot.core.osrm_client import walking_table
    payload = {"code": "NoSegment",
               "message": "Could not find a matching segment for input coordinate 0"}
    client = _FakeClient(payload)
    out = await walking_table(client, _ORIGIN, [dict(c) for c in _CANDIDATES])

    assert all(c["walking_distance_m"] is None for c in out)
    assert all(c["no_route_reason"] == "no_snap" for c in out)


@pytest.mark.asyncio
async def test_walking_table_osrm_down_returns_none():
    from bot.core.osrm_client import walking_table
    client = _FakeClient({}, raises=True)
    assert await walking_table(client, _ORIGIN, [dict(c) for c in _CANDIDATES]) is None


@pytest.mark.asyncio
async def test_nearest_walking_prefers_route_over_straight_line():
    """Ближайший по хаверсину кандидат (c1, ~140м) с маршрутом 812м ПРОИГ-
    РЫВАЕТ дальнему по прямой (c2, ~560м) с маршрутом 430м — в этом смысл
    walkability: река/трасса режут кратчайшую прямую."""
    from bot.core.osrm_client import nearest_walking
    payload = {"code": "Ok",
               "distances": [[0.0, 812.5, 430.0]],
               "durations": [[0.0, 610.0, 330.0]]}
    client = _FakeClient(payload)
    best = await nearest_walking(client, _ORIGIN, [dict(c) for c in _CANDIDATES])

    assert best is not None
    assert best["name"] == "POI дальний по прямой"
    assert best["walking_distance_m"] == 430.0
    assert best["haversine_distance_m"] > 0
    assert best["ratio"] == pytest.approx(430.0 / best["haversine_distance_m"])
    # c2 почти по прямой идёт -> ratio < 1.5 -> барьера нет
    assert best["barrier"] is False


@pytest.mark.asyncio
async def test_nearest_walking_flags_barrier():
    """ratio > BARRIER_RATIO (1.5) -> barrier=True: маршрут сильно длиннее
    прямой, вероятен физический барьер."""
    from bot.core.osrm_client import nearest_walking, BARRIER_RATIO
    payload = {"code": "Ok",
               "distances": [[0.0, 812.5]],
               "durations": [[0.0, 610.0]]}
    client = _FakeClient(payload)
    best = await nearest_walking(client, _ORIGIN, [dict(_CANDIDATES[0])])

    assert best["ratio"] > BARRIER_RATIO
    assert best["barrier"] is True


@pytest.mark.asyncio
async def test_nearest_walking_all_unroutable_returns_no_route_row():
    """Маршрутов нет ни до одного кандидата — строка всё равно возвращается
    (walking=None, haversine ближайшего по прямой, no_route_reason) —
    «попытались, вот что знаем» (Unknown ≠ average)."""
    from bot.core.osrm_client import nearest_walking
    payload = {"code": "NoSegment", "message": "no segment"}
    client = _FakeClient(payload)
    best = await nearest_walking(client, _ORIGIN, [dict(c) for c in _CANDIDATES])

    assert best is not None
    assert best["walking_distance_m"] is None
    assert best["ratio"] is None
    assert best["barrier"] is None
    assert best["no_route_reason"] == "no_snap"
    assert best["name"] == "POI ближний по прямой"  # min по хаверсину


@pytest.mark.asyncio
async def test_nearest_walking_osrm_down_returns_none():
    from bot.core.osrm_client import nearest_walking
    client = _FakeClient({}, raises=True)
    assert await nearest_walking(client, _ORIGIN, [dict(c) for c in _CANDIDATES]) is None


@pytest.mark.asyncio
async def test_check_osrm_alive_and_dead(monkeypatch):
    import bot.core.osrm_client as oc

    class _AliveClient(_FakeClient):
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(oc.httpx, "AsyncClient",
                        lambda *a, **kw: _AliveClient({"code": "Ok"}))
    assert await oc.check_osrm() is True

    monkeypatch.setattr(oc.httpx, "AsyncClient",
                        lambda *a, **kw: _AliveClient({}, raises=True))
    assert await oc.check_osrm() is False
