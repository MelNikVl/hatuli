"""Audit safety checks without an application database or migrations."""
import asyncio
import sys
from types import SimpleNamespace

import pytest

from scripts.audit_price_forecast import audit


class FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class FakeConnection:
    def __init__(self, read_only="on", failure=False):
        self.read_only = read_only
        self.failure = failure
        self.closed = False
        self.queries = []
        self.transaction_options = None

    def transaction(self, **options):
        self.transaction_options = options
        return FakeTransaction()

    async def fetchval(self, sql):
        return self.read_only if sql.startswith("SHOW") else "2026-10-13T05:00:00Z"

    async def fetch(self, sql):
        self.queries.append(sql)
        if self.failure:
            raise RuntimeError("database unavailable")
        return [{"aggregate_count": 42}]

    async def close(self):
        self.closed = True


def connect_fake(monkeypatch, connection):
    captured = {}

    async def connect(dsn, **options):
        captured.update(dsn=dsn, **options)
        return connection

    monkeypatch.setitem(sys.modules, "asyncpg", SimpleNamespace(connect=connect))
    return captured


def test_audit_enforces_two_layers_of_read_only_and_closes(monkeypatch):
    connection = FakeConnection()
    captured = connect_fake(monkeypatch, connection)
    report = asyncio.run(audit("postgresql://unused@127.0.0.1:1/isolated"))
    assert captured["server_settings"]["default_transaction_read_only"] == "on"
    assert connection.transaction_options == {"readonly": True, "isolation": "repeatable_read"}
    assert report["transaction_read_only"] == "on"
    assert connection.closed
    assert connection.queries


def test_audit_fails_closed_if_database_does_not_confirm_read_only(monkeypatch):
    connection = FakeConnection(read_only="off")
    connect_fake(monkeypatch, connection)
    with pytest.raises(RuntimeError, match="read-only"):
        asyncio.run(audit("postgresql://unused@127.0.0.1:1/isolated"))
    assert not connection.queries
    assert connection.closed


def test_audit_closes_failed_read(monkeypatch):
    connection = FakeConnection(failure=True)
    connect_fake(monkeypatch, connection)
    with pytest.raises(RuntimeError, match="unavailable"):
        asyncio.run(audit("postgresql://unused@127.0.0.1:1/isolated"))
    assert connection.closed
