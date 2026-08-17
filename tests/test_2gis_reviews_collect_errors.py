"""Регрессия для задачи 2026-08-17 ("ложный sentinel в reviews pipeline"):
find_geo_id/fetch_reviews (2gis_reviews_collect.py) раньше ловили ЛЮБОЕ
исключение и молча возвращали None/[] — reviews_pipeline.py не мог
отличить "успешно проверили, отзывов нет" от "запрос вообще не удался"
(timeout/5xx/429/ошибка парсинга), и писал sentinel «отзывов нет» в
обоих случаях — ЖК с временным сбоем не проверялся заново ~25 дней.

Эти тесты бьют по 2gis_reviews_collect.py напрямую (мокается только
urllib.request.urlopen — get()/find_geo_id/fetch_reviews реальные, не
замоканы) — reviews_pipeline.py-уровень (собственно решение "писать
sentinel или нет") покрыт отдельно в tests/test_reviews_pipeline.py."""
import importlib
import os
import socket
import sys
import urllib.error
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

mod = importlib.import_module("2gis_reviews_collect")


def _html_with_geo(gid="123", title="ЖК Тестовый Комплекс"):
    # norm() (2gis_reviews_collect.py) отрезает префикс "жк" и требует
    # len(n) >= 5 после этого — короткое "ЖК Тест" ("тест", 4 симв.)
    # НЕ матчится сам по себе (существующее поведение find_geo_id, не
    # то, что здесь тестируется), поэтому фикстура длиннее.
    return f'<a href="/astana/geo/{gid}">{title}</a>'


def _html_no_match():
    return '<html><body>ничего не найдено</body></html>'


class _FakeResponse:
    def __init__(self, text: str):
        self._text = text.encode("utf-8")

    def read(self):
        return self._text

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ── Исход 1: успешный ответ, совпадение есть ─────────────────────────────

def test_find_geo_id_success_with_match():
    with patch.object(mod.urllib.request, "urlopen", return_value=_FakeResponse(_html_with_geo())):
        result = mod.find_geo_id("ЖК Тестовый Комплекс")
    assert result == ("123", "ЖК Тестовый Комплекс")


# ── Исход 1b: успешный ответ, БЕЗ совпадения (это НЕ ошибка) ─────────────

def test_find_geo_id_success_no_match_returns_none_not_raises():
    with patch.object(mod.urllib.request, "urlopen", return_value=_FakeResponse(_html_no_match())):
        result = mod.find_geo_id("ЖК Совсем Другое Название")
    assert result is None  # успешно проверили — не найдено, НЕ исключение


def test_fetch_reviews_success_empty_returns_empty_list():
    with patch.object(mod.urllib.request, "urlopen", return_value=_FakeResponse("<html>нет отзывов</html>")):
        result = mod.fetch_reviews("123")
    assert result == []


# ── Исход 2: временная ошибка (timeout/5xx/429) — ПОДНИМАЕТ TransientFetchError ──

def test_find_geo_id_raises_transient_on_timeout():
    with patch.object(mod.urllib.request, "urlopen", side_effect=socket.timeout("timed out")), \
         patch.object(mod.time, "sleep"):  # retry backoff — не ждём реально в тесте
        try:
            mod.find_geo_id("ЖК Любой")
            assert False, "ожидался TransientFetchError"
        except mod.TransientFetchError:
            pass


def test_get_raises_transient_on_http_500_after_retries():
    err = urllib.error.HTTPError("http://x", 500, "Internal Server Error", {}, None)
    with patch.object(mod.urllib.request, "urlopen", side_effect=err), \
         patch.object(mod.time, "sleep") as sleep_mock:
        try:
            mod.get("http://x")
            assert False, "ожидался TransientFetchError"
        except mod.TransientFetchError:
            pass
    assert sleep_mock.call_count == 2  # retries=2 по умолчанию — backoff реально пытался повторить


def test_get_raises_transient_on_429():
    err = urllib.error.HTTPError("http://x", 429, "Too Many Requests", {}, None)
    with patch.object(mod.urllib.request, "urlopen", side_effect=err), \
         patch.object(mod.time, "sleep"):
        try:
            mod.get("http://x")
            assert False, "ожидался TransientFetchError"
        except mod.TransientFetchError:
            pass


def test_fetch_reviews_raises_transient_on_connection_error():
    with patch.object(mod.urllib.request, "urlopen", side_effect=urllib.error.URLError("connection refused")), \
         patch.object(mod.time, "sleep"):
        try:
            mod.fetch_reviews("123")
            assert False, "ожидался TransientFetchError"
        except mod.TransientFetchError:
            pass


# ── Исход 3: постоянная ошибка (4xx кроме 429) — PermanentFetchError, БЕЗ retry ──

def test_get_raises_permanent_on_403_without_retry():
    err = urllib.error.HTTPError("http://x", 403, "Forbidden", {}, None)
    with patch.object(mod.urllib.request, "urlopen", side_effect=err) as urlopen_mock, \
         patch.object(mod.time, "sleep") as sleep_mock:
        try:
            mod.get("http://x")
            assert False, "ожидался PermanentFetchError"
        except mod.PermanentFetchError:
            pass
    assert urlopen_mock.call_count == 1  # НЕ повторяли — retry бессмысленен для 4xx
    assert sleep_mock.call_count == 0


def test_find_geo_id_raises_permanent_on_404():
    err = urllib.error.HTTPError("http://x", 404, "Not Found", {}, None)
    with patch.object(mod.urllib.request, "urlopen", side_effect=err):
        try:
            mod.find_geo_id("ЖК Любой")
            assert False, "ожидался PermanentFetchError"
        except mod.PermanentFetchError:
            pass


# ── Исход 2 (продолжение): retry-with-backoff реально восстанавливается ──

def test_get_retries_and_succeeds_on_second_attempt():
    """Первая попытка — timeout, вторая — успех: get() должен вернуть
    результат, не поднимать исключение (задача: "назначить retry с
    backoff" — не просто зафиксировать неудачу, а реально попробовать
    ещё раз)."""
    calls = {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise socket.timeout("timed out")
        return _FakeResponse("<html>OK</html>")

    with patch.object(mod.urllib.request, "urlopen", side_effect=flaky), \
         patch.object(mod.time, "sleep") as sleep_mock:
        result = mod.get("http://x")
    assert result == "<html>OK</html>"
    assert calls["n"] == 2
    assert sleep_mock.call_count == 1  # backoff перед повторной попыткой
