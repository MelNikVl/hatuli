"""Maintenance safety tests: memory transactions, mocked sources, no DB/network."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from scripts import rental_stage0_maintenance as tool

NOW = datetime(2026, 10, 16, 8, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused:unused@127.0.0.1:1/maintenance_test")
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    from bot.db import pg
    denied = AsyncMock(side_effect=AssertionError("No application pools, migrations or database connections"))
    monkeypatch.setattr("asyncpg.connect", denied)
    monkeypatch.setattr("asyncpg.create_pool", denied)
    monkeypatch.setattr(pg, "init_pool", denied)
    monkeypatch.setattr("bot.core.rental_stage0_identity.resolve_rental_complex", AsyncMock(return_value=10))


def rental(**extra):
    return {**dict.fromkeys(tool.RENTAL_FIELDS), "id": "10001", "title": "2-комнатная, аренда в месяц",
            "price": 250000, "area": 60, "rooms": 2, "city": "astana", "prop_type": "apartment",
            "rent_period": "unknown", "rent_period_conflict": False, "lat": 51.12, "lon": 71.43,
            "complex_url": "https://krisha.kz/complex/show/astana/test", "is_active": True,
            "last_seen": NOW-timedelta(hours=1), **extra}


def sale(**extra):
    return {**dict.fromkeys(tool.SALE_FIELDS), "id": "20001", "price": 40000000, "area": 60, "rooms": 2,
            "city": "astana", "district": "Есиль", "complex_name": "Название", "complex_id": 10,
            "complex_resolution": "url", "is_active": True, "last_seen": NOW-timedelta(hours=1),
            "est_rent": 300000, "yield_pct": Decimal("9"), "net_yield_pct": 7.5, "payback_years": Decimal("11.1"),
            "rent_source": "старый расчёт", **extra}


def index(**extra):
    return {"median_price": 280000, "price_per_sqm": 5000, "p25_price": 250000, "p75_price": 310000,
            "sample_count": 10, "level": "ЖК", "policy_version": "3", "rent_period": "monthly", "window_days": 30,
            "updated_at": NOW-timedelta(minutes=5), "source_oldest_at": NOW-timedelta(hours=4),
            "source_newest_at": NOW-timedelta(hours=1), **extra}


class Connection:
    def __init__(self, rows=None, missing=False):
        self.rows = {row["id"]: deepcopy(row) for row in rows or []}
        self.settings = {"HEX_EDGE_M": "50"}
        self.calls = []
        self.transaction_options = None
        self.missing = missing
        self.closed = False
        self.database_name = "isolated_test"
        self.schema_name = "public"

    def transaction(self, **options):
        connection = self
        self.transaction_options = options
        class Transaction:
            async def __aenter__(self):
                self.before = deepcopy(connection.rows)
            async def __aexit__(self, exc_type, *args):
                if exc_type:
                    connection.rows = self.before
        return Transaction()

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        if sql.startswith("SHOW"):
            return "on" if self.transaction_options["readonly"] else "off"
        return NOW

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        if "information_schema.columns" in sql:
            return [{"table_name": table, "column_name": field} for table, fields in tool.REQUIRED.items()
                    for field in fields if not (self.missing and field == "rent_period")]
        if "SELECT key,value FROM app_settings" in sql:
            return [{"key": key, "value": value} for key, value in sorted(self.settings.items())]
        if sql.startswith("SELECT id,"):
            return [deepcopy(row) for identifier, row in sorted(self.rows.items()) if identifier > args[0]][:args[1]]
        return []

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        if "current_database()" in sql:
            return {"database": self.database_name, "schema": self.schema_name, "server": "127.0.0.1", "port": 1}
        return deepcopy(self.rows.get(args[0]))

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        if sql.startswith("UPDATE"):
            identifier, *values = args
            operation = "backfill" if "rental_listings" in sql else "recompute"
            self.rows[identifier].update(zip(tool.WRITABLE[operation][2], values))
            return "UPDATE 1"
        return "SELECT 1"

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("title,old,flag,expected,conflict", [
    ("Квартира 15000 ₸", "unknown", False, "unknown", False),
    ("Квартира 300000 ₸", "unknown", False, "unknown", False),
    ("Квартира за сутки", "unknown", False, "daily", False),
    ("Квартира в месяц", "unknown", False, "monthly", False),
    ("Квартира за сутки", "monthly", False, "unknown", True),
    ("Квартира в месяц", "daily", False, "unknown", True),
    ("Квартира в месяц", "unknown", True, "unknown", True),
    ("Квартира за сутки и в месяц", "unknown", False, "unknown", True),
])
async def test_backfill_uses_explicit_title_never_amount_or_unrecorded_catalog(title, old, flag, expected, conflict):
    after, evidence = await tool.backfill_proposal(rental(title=title, rent_period=old, rent_period_conflict=flag), {})
    assert after["rent_period"] == expected and after["rent_period_conflict"] is conflict
    assert "price" not in evidence and "catalog" not in evidence


@pytest.mark.asyncio
async def test_backfill_uses_read_only_identity_and_observed_coordinates_no_nearest(monkeypatch):
    from bot.core import rental_stage0_identity as identity
    resolver = AsyncMock(return_value=99)
    monkeypatch.setattr(identity, "resolve_rental_complex", resolver)
    after, evidence = await tool.backfill_proposal(rental(address="PRIVATE-STREET"), {"HEX_EDGE_M": "100"})
    assert after["complex_id"] == 99 and after["hex_id"] and after["hex_edge_m"] == 100
    assert "lat" not in resolver.await_args.kwargs and "lon" not in resolver.await_args.kwargs
    assert "PRIVATE-STREET" not in json.dumps(evidence)
    cleared, _ = await tool.backfill_proposal(rental(lat=None, lon=None), {})
    assert cleared["hex_id"] is None and cleared["hex_edge_m"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate,status", [(None, "existing_binding_preserved_unresolved"),
    (99, "existing_binding_requires_review"), (42, "existing_binding_confirmed")])
async def test_backfill_never_erases_or_overwrites_an_existing_possible_manual_identity(monkeypatch, candidate, status):
    monkeypatch.setattr("bot.core.rental_stage0_identity.resolve_rental_complex", AsyncMock(return_value=candidate))
    after, evidence = await tool.backfill_proposal(rental(complex_id=42, complex_url=None), {})
    assert after["complex_id"] == 42 and evidence["identity_status"] == status
    assert evidence["resolved_complex_id"] == candidate


@pytest.mark.asyncio
@pytest.mark.parametrize("edge", ["nan", "inf", "0", "501", "broken"])
async def test_invalid_hex_setting_refuses_plan_instead_of_inventing_geo(edge):
    with pytest.raises(tool.MaintenanceError, match="HEX_EDGE_M"):
        await tool.backfill_proposal(rental(), {"HEX_EDGE_M": edge})


@pytest.mark.asyncio
async def test_recompute_uses_fresh_index_and_settings_without_changing_deal_score(monkeypatch):
    lookup = AsyncMock(return_value=index())
    monkeypatch.setattr("bot.core.rental_parser.lookup_rental_estimate", lookup)
    after, evidence = await tool.recompute_proposal(sale(), {"NET_YIELD_VACANCY": ".9", "NET_YIELD_OPEX_SHARE": ".2", "NET_YIELD_PURCHASE_COST": "1.1"}, NOW)
    assert after == {"est_rent": 300000, "yield_pct": 9.0, "net_yield_pct": 5.7, "payback_years": 11.1, "rent_source": "ЖК, n=10"}
    assert evidence["asking_prices"] is True and evidence["params_used"]["occupancy_share"] == .9
    assert lookup.await_args.kwargs == {"complex_id": 10}
    assert set(after) == set(tool.WRITABLE["recompute"][2])


@pytest.mark.asyncio
async def test_missing_estimate_clears_legacy_values_to_null(monkeypatch):
    monkeypatch.setattr("bot.core.rental_parser.lookup_rental_estimate", AsyncMock(return_value=None))
    after, evidence = await tool.recompute_proposal(sale(), {}, NOW)
    assert all(value is None for value in after.values())
    assert evidence["reason"] == "no_fresh_policy3_index"


@pytest.mark.asyncio
async def test_segment_cache_reuses_lookup_but_respects_area_and_source_freshness(monkeypatch):
    lookup = AsyncMock(return_value=index())
    monkeypatch.setattr("bot.core.rental_parser.lookup_rental_estimate", lookup)
    cache = {}
    first, _ = await tool.recompute_proposal(sale(), {}, NOW, estimate_cache=cache)
    second, _ = await tool.recompute_proposal(sale(id="20002", area=70, complex_name="Другое написание того же ЖК"), {}, NOW, estimate_cache=cache)
    assert lookup.await_count == 1
    assert first["est_rent"] == 300000 and second["est_rent"] == 350000
    # A cached source must still pass the 72-hour provenance gate.
    later = NOW+timedelta(hours=70)
    expired, _ = await tool.recompute_proposal(sale(last_seen=later-timedelta(hours=1)), {}, later, estimate_cache=cache)
    assert all(value is None for value in expired.values()) and lookup.await_count == 1


@pytest.mark.asyncio
async def test_stale_sale_is_cleared_and_geo_binding_does_not_claim_same_complex(monkeypatch):
    lookup = AsyncMock(return_value=index())
    monkeypatch.setattr("bot.core.rental_parser.lookup_rental_estimate", lookup)
    after, _ = await tool.recompute_proposal(sale(last_seen=NOW-timedelta(hours=73)), {}, NOW)
    assert all(value is None for value in after.values()) and lookup.await_count == 0
    await tool.recompute_proposal(sale(complex_resolution="geo"), {}, NOW)
    assert lookup.await_args.kwargs == {"complex_id": None} and lookup.await_args.args[2] is None


def test_numerically_unchanged_decimal_fields_do_not_create_repeated_updates():
    row = sale()
    after = {key: row[key] for key in tool.WRITABLE["recompute"][2]}
    after.update(yield_pct=9.0, payback_years=11.1)
    assert tool.change_record("recompute", row, after, {}) is None


@pytest.mark.asyncio
async def test_read_adapter_reuses_connection_and_rejects_hidden_dml_or_pool_writes():
    from bot.db import pg, settings
    previous = pg.fetch
    connection = Connection()
    with tool.read_adapter(connection, {"HEX_EDGE_M": "90"}):
        assert settings.get("HEX_EDGE_M") == "90"
        await pg.fetch("SELECT id FROM complexes")
        with pytest.raises(tool.MaintenanceError):
            await pg.fetch("WITH changed AS (DELETE FROM complexes RETURNING id) SELECT * FROM changed")
        with pytest.raises(tool.MaintenanceError):
            pg.get_pool()
        with pytest.raises(tool.MaintenanceError):
            pg.execute("UPDATE complexes SET name='bad'")
    assert pg.fetch is previous
    assert [sql for sql, _ in connection.calls] == ["SELECT id FROM complexes"]


@pytest.mark.asyncio
async def test_build_plan_is_bounded_private_and_does_not_export_source_text():
    connection = Connection([rental(id=str(10000+i), title="PRIVATE-TITLE в месяц", address="PRIVATE-ADDRESS") for i in range(3)])
    plan = await tool.build_plan(connection, "backfill", limit=2)
    assert plan["has_more"] is True and plan["scanned"] == 2 and plan["next_after_id"] == "10001"
    assert len(plan["changes"]) == 2 and plan["database_writes"] == 0
    assert "PRIVATE" not in json.dumps(tool.json_value(plan))
    assert not any(sql.startswith(("UPDATE", "DELETE", "INSERT")) for sql, _ in connection.calls)
    tool.verify_plan(plan, plan["plan_sha256"], "backfill")


@pytest.mark.asyncio
async def test_conflicting_existing_identity_is_reported_even_without_any_proposed_write():
    row = rental(complex_id=42)
    after, _ = await tool.backfill_proposal(row, {})
    row.update(after)
    plan = await tool.build_plan(Connection([row]), "backfill")
    assert plan["changes"] == []
    assert plan["identity_review_required"] == [{"id": "10001", "existing_complex_id": 42, "candidate_complex_id": 10}]


@pytest.mark.asyncio
async def test_legacy_schema_can_be_audited_but_not_modified():
    connection = Connection(missing=True)
    report = await tool.build_plan(connection, "audit")
    assert report["schema"]["ready"] is False and report["changes"] == []
    with pytest.raises(tool.MaintenanceError, match="schema"):
        await tool.build_plan(connection, "backfill")


@pytest.mark.asyncio
async def test_apply_rechecks_rows_and_all_proposals_before_first_update():
    connection = Connection([rental(id="10001"), rental(id="10002")])
    plan = await tool.build_plan(connection, "backfill")
    connection.rows["10002"]["title"] = "A newly observed title"
    with pytest.raises(tool.MaintenanceError, match="source row changed"):
        async with connection.transaction(readonly=False):
            await tool.apply_plan(connection, plan, plan["plan_sha256"], "backfill")
    assert not any(sql.startswith("UPDATE") for sql, _ in connection.calls)


@pytest.mark.asyncio
async def test_apply_rechecks_resolver_evidence_and_settings(monkeypatch):
    connection = Connection([rental()])
    plan = await tool.build_plan(connection, "backfill")
    monkeypatch.setattr("bot.core.rental_stage0_identity.resolve_rental_complex", AsyncMock(return_value=11))
    with pytest.raises(tool.MaintenanceError, match="evidence changed"):
        await tool.apply_plan(connection, plan, plan["plan_sha256"], "backfill")
    connection.settings["HEX_EDGE_M"] = "100"
    with pytest.raises(tool.MaintenanceError, match="Settings changed"):
        await tool.apply_plan(connection, plan, plan["plan_sha256"], "backfill")
    assert not any(sql.startswith("UPDATE") for sql, _ in connection.calls)


@pytest.mark.asyncio
async def test_plan_cannot_be_applied_to_another_database_even_with_identical_rows():
    connection = Connection([rental()])
    plan = await tool.build_plan(connection, "backfill")
    connection.database_name = "another_database"
    with pytest.raises(tool.MaintenanceError, match="different database"):
        await tool.apply_plan(connection, plan, plan["plan_sha256"], "backfill")
    assert not any(sql.startswith("UPDATE") for sql, _ in connection.calls)


@pytest.mark.asyncio
async def test_plan_cannot_be_applied_to_another_schema_even_with_identical_rows():
    connection = Connection([rental()])
    plan = await tool.build_plan(connection, "backfill")
    connection.schema_name = "another_schema"
    with pytest.raises(tool.MaintenanceError, match="different database"):
        await tool.apply_plan(connection, plan, plan["plan_sha256"], "backfill")
    assert not any(sql.startswith("UPDATE") for sql, _ in connection.calls)


@pytest.mark.asyncio
async def test_apply_updates_only_reviewed_columns_under_lock_and_reports_actual_count():
    connection = Connection([rental()])
    plan = await tool.build_plan(connection, "backfill")
    async with connection.transaction(readonly=False, isolation="repeatable_read"):
        report = await tool.apply_plan(connection, plan, plan["plan_sha256"], "backfill")
    assert report["database_writes"] == 1 and report["removed_index_rows"] is None
    assert report["index_rebuild_performed"] is False and report["deal_score_recompute_performed"] is False
    updates = [(sql, args) for sql, args in connection.calls if sql.startswith("UPDATE")]
    assert len(updates) == 1 and "rent_period=$2" in updates[0][0] and "complex_id=$4" in updates[0][0]
    assert any("FOR UPDATE" in sql for sql, _ in connection.calls)
    assert any("pg_advisory_xact_lock" in sql for sql, _ in connection.calls)
    assert connection.rows["10001"]["rent_period"] == "monthly"


@pytest.mark.asyncio
async def test_recompute_apply_clears_missing_estimates_without_zero_or_score_updates(monkeypatch):
    monkeypatch.setattr("bot.core.rental_parser.lookup_rental_estimate", AsyncMock(return_value=None))
    connection = Connection([sale()])
    plan = await tool.build_plan(connection, "recompute")
    async with connection.transaction(readonly=False):
        report = await tool.apply_plan(connection, plan, plan["plan_sha256"], "recompute")
    assert report["database_writes"] == 1 and report["deal_score_recompute_required"] is True
    assert all(connection.rows["20001"][key] is None for key in tool.WRITABLE["recompute"][2])
    sql = next(sql for sql, _ in connection.calls if sql.startswith("UPDATE"))
    assert "score" not in sql.lower() and "price=" not in sql and "last_seen=" not in sql


@pytest.mark.asyncio
async def test_changed_index_evidence_invalidates_sale_plan_even_when_sale_row_unchanged(monkeypatch):
    lookup = AsyncMock(return_value=index())
    monkeypatch.setattr("bot.core.rental_parser.lookup_rental_estimate", lookup)
    connection = Connection([sale()])
    plan = await tool.build_plan(connection, "recompute")
    lookup.return_value = index(updated_at=NOW-timedelta(minutes=1))
    with pytest.raises(tool.MaintenanceError, match="evidence changed"):
        await tool.apply_plan(connection, plan, plan["plan_sha256"], "recompute")
    assert not any(sql.startswith("UPDATE") for sql, _ in connection.calls)


@pytest.mark.asyncio
async def test_mid_batch_database_failure_rolls_back_all_updates():
    connection = Connection([rental(id="10001"), rental(id="10002")])
    plan = await tool.build_plan(connection, "backfill")
    original = connection.execute
    async def fail_second(sql, *args):
        if sql.startswith("UPDATE") and args[0] == "10002":
            raise RuntimeError("isolated write failure")
        return await original(sql, *args)
    connection.execute = fail_second
    before = deepcopy(connection.rows)
    with pytest.raises(RuntimeError, match="isolated write failure"):
        async with connection.transaction(readonly=False):
            await tool.apply_plan(connection, plan, plan["plan_sha256"], "backfill")
    assert connection.rows == before


def test_tampered_or_unbounded_plan_is_rejected():
    plan = tool.seal({"version": 1, "operation": "backfill", "changes": []})
    with pytest.raises(tool.MaintenanceError, match="hash"):
        tool.verify_plan({**plan, "operation": "recompute"}, plan["plan_sha256"], "recompute")
    with pytest.raises(tool.MaintenanceError, match="SHA"):
        tool.verify_plan(plan, "abc", "backfill")
    unbounded = tool.seal({**plan, "changes": [{}]*1001})
    with pytest.raises(tool.MaintenanceError, match="unbounded"):
        tool.verify_plan(unbounded, unbounded["plan_sha256"], "backfill")


def test_private_artifact_hash_and_mode_and_static_refusal(tmp_path):
    path = tmp_path/"private"/"plan.json"
    tool.private_json(path, tool.seal({"version": 1, "operation": "audit", "changes": []}))
    assert tool.load_plan(path)["plan_sha256"]
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(tool.MaintenanceError):
        tool.private_json(tmp_path/"static"/"plan.json", {})
    assert not (tmp_path/"static").exists()


@pytest.mark.asyncio
async def test_run_default_connection_and_transaction_are_read_only_and_never_use_ambient_dsn(tmp_path):
    env = tmp_path/"explicit.env"
    env.write_text("DATABASE_URL=postgresql://unused:unused@127.0.0.1:1/explicit_test\n", encoding="utf-8")
    connection = Connection([rental()])
    connect = AsyncMock(return_value=connection)
    args = SimpleNamespace(operation="backfill", apply=False, plan=None, expected_plan_sha=None,
                           env_file=env, output=tmp_path/"plan.json", limit=500, after_id="")
    result = await tool.run(args, connect=connect)
    assert connect.await_args.args == ("postgresql://unused:unused@127.0.0.1:1/explicit_test",)
    assert connect.await_args.kwargs["server_settings"]["default_transaction_read_only"] == "on"
    assert connection.transaction_options == {"readonly": True, "isolation": "repeatable_read"}
    assert connection.closed and result["database_writes"] == 0
    assert "DATABASE_URL" not in args.output.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_apply_without_exact_review_or_private_output_never_connects(tmp_path):
    connection = AsyncMock()
    args = SimpleNamespace(operation="backfill", apply=True, plan=None, expected_plan_sha=None,
                           env_file=tmp_path/"not-read", output=tmp_path/"report.json", limit=500, after_id="")
    with pytest.raises(tool.MaintenanceError):
        await tool.run(args, connect=connection)
    connection.assert_not_awaited()


@pytest.mark.asyncio
async def test_wrong_database_readonly_state_is_refused_before_any_audit(tmp_path):
    env = tmp_path/"explicit.env"
    env.write_text("DATABASE_URL=postgresql://unused:unused@127.0.0.1:1/explicit_test\n", encoding="utf-8")
    connection = Connection()
    original = connection.fetchval
    async def wrong_mode(sql, *args):
        return "off" if sql.startswith("SHOW") else await original(sql, *args)
    connection.fetchval = wrong_mode
    args = SimpleNamespace(operation="audit", apply=False, plan=None, expected_plan_sha=None,
                           env_file=env, output=tmp_path/"audit.json", limit=500, after_id="")
    with pytest.raises(tool.MaintenanceError, match="read-only"):
        await tool.run(args, connect=AsyncMock(return_value=connection))
    assert connection.closed and connection.calls == [] and not args.output.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [OSError("private-path"), ValueError("private-serialization")])
async def test_report_failure_after_commit_explicitly_reports_applied_state(monkeypatch, tmp_path, error):
    env = tmp_path/"explicit.env"
    env.write_text("DATABASE_URL=postgresql://unused:unused@127.0.0.1:1/explicit_test\n", encoding="utf-8")
    connection = Connection([rental()])
    plan = await tool.build_plan(connection, "backfill")
    plan_path = tmp_path/"plan.json"
    tool.private_json(plan_path, plan)
    def fail_report(*args):
        raise error
    monkeypatch.setattr(tool, "private_json", fail_report)
    args = SimpleNamespace(operation="backfill", apply=True, plan=plan_path, expected_plan_sha=plan["plan_sha256"],
                           env_file=env, output=tmp_path/"report.json", limit=500, after_id="")
    with pytest.raises(tool.MaintenanceError, match="database batch committed") as caught:
        await tool.run(args, connect=AsyncMock(return_value=connection))
    assert "private-" not in str(caught.value)
    assert connection.closed and connection.rows["10001"]["rent_period"] == "monthly"


def test_cli_requires_explicit_env_file_and_never_prints_exception_contents(monkeypatch, capsys):
    with pytest.raises(SystemExit):
        tool.parser().parse_args(["audit", "--output", "audit.json"])
    capsys.readouterr()
    monkeypatch.setattr(tool, "run", AsyncMock(side_effect=RuntimeError("PRIVATE-DSN-AND-CONTACT")))
    assert tool.main(["audit", "--env-file", "placeholder.env", "--output", "audit.json"]) == 1
    assert "PRIVATE" not in capsys.readouterr().err
