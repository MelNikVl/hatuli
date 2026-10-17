"""Offline proof that the rental-plan fix changes evaluation, not membership."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.core.complex_membership import rental_complex_match_sql, rental_identity_ctes_sql
from bot.core.comparison_rental_estimate import RENTAL_COMPARISON_SQL, SINGLE_OFFER_RENTAL_SQL
from scripts import publish_market_comparison as publisher


def test_materialized_identity_is_the_identical_shared_membership_query():
    original = rental_identity_ctes_sql()
    materialized = rental_identity_ctes_sql(materialize_roots=True)
    assert materialized.count("rental_roots AS MATERIALIZED (") == 1
    # This equality proves all redirect, ambiguity, reviewed-name/source and
    # child/umbrella predicates survive byte-for-byte; only evaluation changes.
    assert materialized.replace("rental_roots AS MATERIALIZED (", "rental_roots AS (") == original
    assert RENTAL_COMPARISON_SQL.startswith(materialized)
    assert SINGLE_OFFER_RENTAL_SQL.startswith(original)
    assert "rental_roots AS MATERIALIZED (" not in SINGLE_OFFER_RENTAL_SQL
    assert "rental_roots AS MATERIALIZED (" not in rental_complex_match_sql()


def test_full_and_single_offer_queries_keep_all_source_period_and_scope_gates():
    for sql in (RENTAL_COMPARISON_SQL, SINGLE_OFFER_RENTAL_SQL):
        for gate in (
            "r.complex_id IS NULL AND nk.n=complex_name_key(r.complex_name)",
            "canonical.id=coalesce(bound.cid,nk.cid)",
            "source.cid=canonical.id OR (r.complex_id IS NOT NULL AND source.cid=canonical.parent_id)",
            "r.rent_period='monthly'", "NOT coalesce(r.rent_period_conflict,false)",
            "NOT coalesce(r.is_duplicate,false)", "NOT coalesce(r.dup_needs_review,false)",
            "nullif(btrim(r.duplicate_of),'') IS NULL AND r.archived_at IS NULL",
            "r.last_seen BETWEEN $1::timestamptz-interval '72 hours' AND $1",
            "NOT coalesce(canonical.is_umbrella,false)",
        ):
            assert gate in sql
    assert "bound.id=$2" in SINGLE_OFFER_RENTAL_SQL
    assert "r.rooms=$3" in SINGLE_OFFER_RENTAL_SQL
    assert "r.area BETWEEN $4::double precision*0.85 AND $4::double precision*1.15" in SINGLE_OFFER_RENTAL_SQL
    assert "LIMIT 200" in SINGLE_OFFER_RENTAL_SQL
    assert "LIMIT " not in RENTAL_COMPARISON_SQL


@pytest.mark.asyncio
async def test_publisher_uses_optimized_query_inside_unchanged_readonly_transaction(monkeypatch):
    now = datetime(2026, 10, 17, 3, tzinfo=timezone.utc)
    class Transaction:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
    connection = SimpleNamespace(fetch=AsyncMock(return_value=[]), fetchrow=AsyncMock(return_value={}),
                                 fetchval=AsyncMock(return_value="50"), close=AsyncMock())
    def transaction(**kwargs):
        assert kwargs == {"isolation": "repeatable_read", "readonly": True}
        return Transaction()
    connection.transaction = transaction
    connect = AsyncMock(return_value=connection)
    monkeypatch.setattr("asyncpg.connect", connect)
    monkeypatch.setattr(publisher, "load_daily_picks", lambda: None)
    result = await publisher.read_market_rows("postgresql://unused:unused@127.0.0.1:1/never_connect", now)
    assert result[:2] == ([], [])
    rental_calls = [call for call in connection.fetch.call_args_list if call.args[0] == RENTAL_COMPARISON_SQL]
    assert len(rental_calls) == 1 and rental_calls[0].args[1] == now
    assert "rental_roots AS MATERIALIZED (" in rental_calls[0].args[0]
    assert connect.call_args.kwargs["command_timeout"] == 60
    assert connect.call_args.kwargs["server_settings"]["default_transaction_read_only"] == "on"
    connection.close.assert_awaited_once()
