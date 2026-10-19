"""Standalone exporter safety, with an injected mock connection only."""
from datetime import datetime, timezone
import json
import os

import pytest

from scripts.export_rental_editorial_slice import export_slice, write_private
from bot.core.rental_editorial_slice import MAX_SCAN


class Transaction:
    async def __aenter__(self): return self
    async def __aexit__(self,*args): pass


class Connection:
    def __init__(self, *, read_only="on", isolation="repeatable read", count=0, overflow=False):
        self.read_only=read_only;self.isolation=isolation;self.count=count;self.overflow=overflow
        self.queries=[];self.closed=False;self.tx=None
    def transaction(self,**kwargs):
        self.tx=kwargs
        return Transaction()
    async def fetchval(self,sql):
        self.queries.append(sql)
        if sql=="SHOW transaction_read_only": return self.read_only
        if sql=="SHOW transaction_isolation": return self.isolation
        if sql=="SELECT now()": return datetime(2026,10,19,7,tzinfo=timezone.utc)
        if "count(*)" in sql: return self.count
        raise AssertionError(sql)
    async def fetch(self,sql,*args):
        self.queries.append(sql)
        if self.overflow and "FROM complexes ORDER BY" in sql: return [{}]*(args[0])
        return []
    async def close(self): self.closed=True


def connector(conn,calls):
    async def connect(dsn,**kwargs):
        assert dsn=="postgresql://unused:unused@127.0.0.1:1/hatuli_test"
        calls.append(kwargs)
        return conn
    return connect


@pytest.mark.asyncio
async def test_injected_connection_readonly_repeatable_bounds_and_no_execute():
    conn=Connection();calls=[]
    result=await export_slice("postgresql://unused:unused@127.0.0.1:1/hatuli_test",connect=connector(conn,calls))
    assert result["database_writes"]==0 and result["complete_database_scan"]
    assert conn.tx=={"readonly":True,"isolation":"repeatable_read"}
    assert calls[0]["server_settings"]["default_transaction_read_only"]=="on"
    assert calls[0]["server_settings"]["statement_timeout"]=="20000"
    assert all(q.lstrip().startswith(("SELECT","SHOW","WITH")) for q in conn.queries)
    assert conn.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("options",[{"read_only":"off"},{"isolation":"read committed"}])
async def test_missing_readonly_or_repeatability_fails_before_population(options):
    conn=Connection(**options)
    with pytest.raises(RuntimeError):
        await export_slice("postgresql://unused:unused@127.0.0.1:1/hatuli_test",connect=connector(conn,[]))
    assert not any("rental_listings" in q for q in conn.queries)
    assert conn.closed


@pytest.mark.asyncio
async def test_population_overflow_refuses_before_rows_or_identity_queries():
    conn=Connection(count=MAX_SCAN+1)
    with pytest.raises(ValueError,match="export refused"):
        await export_slice("postgresql://unused:unused@127.0.0.1:1/hatuli_test",connect=connector(conn,[]))
    assert len(conn.queries)==4 and conn.closed


@pytest.mark.asyncio
async def test_evidence_overflow_and_count_mismatch_refuse_complete_claim():
    for conn in (Connection(overflow=True),Connection(count=1)):
        with pytest.raises(ValueError,match="Incomplete"):
            await export_slice("postgresql://unused:unused@127.0.0.1:1/hatuli_test",connect=connector(conn,[]))
        assert conn.closed


def test_private_atomic_no_clobber_and_no_partial_file_on_invalid_json(tmp_path):
    target=tmp_path/"private"/"slice.json"
    meta=write_private(target,{"value":1})
    assert len(meta["sha256"])==64
    assert json.loads(target.read_text())=={"value":1}
    if os.name!="nt": assert target.stat().st_mode & 0o777==0o600
    old=target.read_bytes()
    with pytest.raises(ValueError,match="preserved"): write_private(target,{"value":2})
    assert target.read_bytes()==old
    invalid=target.with_name("invalid.json")
    with pytest.raises(ValueError): write_private(invalid,{"value":float("nan")})
    assert not invalid.exists()
    assert not list(target.parent.glob(".rental-editorial-*"))
