"""Independent runtime importer verification: all PostgreSQL operations mocked."""
from argparse import Namespace
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import sys
from types import SimpleNamespace

import pytest

from scripts import import_browser_listing_details as module


def data():
    now = datetime.now(timezone.utc)
    before = {"price": 25_000_000, "rooms": 1, "area": 38.400001525878906,
              "photo_count": 0, "floor": None, "floors_total": None,
              "year_built": None, "last_seen": (now-timedelta(hours=5)).isoformat()}
    record = {"listing_id": "123456789", "source_listing_id": "123456789",
              "url": "https://krisha.kz/a/show/123456789", "checked_at": now.isoformat(),
              "source_active": True, "source_price": before["price"], "source_rooms": 1,
              "source_area": 38.4, "photos": ["https://astps-photos-kr.kcdn.kz/webp/photo.jpg"],
              "floor": 5, "floors_total": 9, "year_built": 2020, "before": before}
    row = {"id": record["listing_id"], "url": record["url"],
           **{key: before[key] for key in ("price", "rooms", "area", "floor", "floors_total", "year_built")},
           "last_seen": module.timestamp(before["last_seen"]), "photos": None,
           "is_active": True, "is_duplicate": False, "dup_needs_review": False, "archived_at": None}
    return record, row


class Connection:
    def __init__(self, row):
        self.rows = {row["id"]: deepcopy(row)}
        self.calls = []
        self.closed = False
        self.committed = False
        self.fail_update = False

    def transaction(self, **kwargs):
        self.transaction_kwargs = kwargs
        connection = self
        class Transaction:
            async def __aenter__(self):
                connection.saved_rows = deepcopy(connection.rows)
                connection.in_transaction = True
            async def __aexit__(self, typ, val, traceback):
                connection.in_transaction = False
                if typ:
                    connection.rows = connection.saved_rows
                else:
                    connection.committed = True
        return Transaction()

    async def fetchrow(self, query, identifier):
        self.calls.append((query, (identifier,)))
        return deepcopy(self.rows.get(identifier))

    async def fetchval(self, query, *args):
        self.calls.append((query, args))
        if query == "SHOW transaction_read_only":
            return "on" if self.transaction_kwargs["readonly"] else "off"
        if "current_database()" in query:
            return "mock:local:local"
        assert query.startswith("UPDATE apartment_listings SET ")
        assert self.transaction_kwargs["readonly"] is False
        if self.fail_update:
            raise RuntimeError("simulated concurrent/failing update")
        assignments = query.split(" SET ", 1)[1].split(" WHERE ", 1)[0]
        fields = re.findall(r"(\w+)=\$\d+", assignments)
        identifier = args[len(fields)]
        for field, value in zip(fields, args):
            assert field in module.FIELDS
            self.rows[identifier][field] = json.loads(value) if field == "photos" and value is not None else value
        return identifier

    async def close(self):
        self.closed = True


def prepare(tmp_path, monkeypatch, *, apply=False, rollback=False):
    record, row = data()
    connection = Connection(row)
    async def connect(dsn, *, command_timeout, server_settings):
        assert dsn == "postgresql://never-connect/unused"
        assert command_timeout == 60
        assert server_settings["default_transaction_read_only"] == ("off" if apply else "on")
        assert server_settings["lock_timeout"] == "30000"
        assert server_settings["statement_timeout"] == "60000"
        connection.connected = True
        connection.server_settings = server_settings
        return connection
    monkeypatch.setitem(sys.modules, "asyncpg", SimpleNamespace(connect=connect))
    monkeypatch.setenv("DATABASE_URL", "postgresql://never-connect/unused")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"schema_version": 1, "records": [record]}), encoding="utf8")
    args = Namespace(manifest=str(manifest) if not rollback else None,
                     rollback_manifest=str(manifest) if rollback else None,
                     output=str(tmp_path / "receipt.json"), source_checks_output=str(tmp_path / "checks.json"),
                     apply=apply, env_file=None)
    return args, record, connection


@pytest.mark.asyncio
async def test_runtime_default_is_repeatable_read_readonly_and_no_update(tmp_path, monkeypatch):
    args, record, connection = prepare(tmp_path, monkeypatch)
    await module.run(args)
    assert connection.transaction_kwargs == {"isolation": "repeatable_read", "readonly": True}
    assert not any(query.startswith("UPDATE") or "FOR UPDATE" in query for query, _ in connection.calls)
    receipt = json.loads((tmp_path / "receipt.json").read_text())
    assert receipt["summary"] == {"planned": 1}
    assert receipt["transaction_status"] == "read_only"
    assert not (tmp_path / "checks.json").exists()
    assert connection.closed


@pytest.mark.asyncio
async def test_apply_locked_fields_receipt_written_before_commit(tmp_path, monkeypatch):
    args, record, connection = prepare(tmp_path, monkeypatch, apply=True)
    original_writer = module.atomic_json
    events = []
    def writer(path, report):
        if "transaction_status" in report:
            events.append((report["transaction_status"], connection.in_transaction,
                           connection.committed, report.get("source_checks_status")))
        original_writer(path, report)
    monkeypatch.setattr(module, "atomic_json", writer)
    await module.run(args)
    assert connection.transaction_kwargs == {"isolation": "repeatable_read", "readonly": False}
    assert any("FOR UPDATE" in query for query, _ in connection.calls)
    assert events == [("prepared", True, False, None),
                      ("committed", False, True, None),
                      ("committed", False, True, "published")]
    assert connection.rows[record["listing_id"]]["price"] == record["before"]["price"]
    assert connection.rows[record["listing_id"]]["last_seen"] == module.timestamp(record["before"]["last_seen"])
    receipt = json.loads((tmp_path / "receipt.json").read_text())
    assert receipt["summary"] == {"applied": 1}
    assert receipt["transaction_status"] == "committed"
    assert receipt["source_checks_status"] == "published" and receipt["source_checks_records"] == 1
    assert set(receipt["rows"][0]["after"]) == set(module.FIELDS)
    checks = json.loads((tmp_path / "checks.json").read_text())
    assert checks["records"][0]["checked_at"] == record["checked_at"]
    assert "last_seen" not in checks["records"][0]
    assert connection.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["malformed_existing_cache", "source_disk_failure"])
async def test_post_commit_source_publisher_failure_records_partial_status_without_claiming_database_rollback(tmp_path, monkeypatch, failure):
    args, record, connection = prepare(tmp_path, monkeypatch, apply=True)
    path = tmp_path / "checks.json"
    if failure == "malformed_existing_cache":
        path.write_text("{invalid existing source cache", encoding="utf8")
        expected_exception = ValueError
        expected_message = "malformed"
    else:
        old = {key: record[key] for key in ("listing_id", "source_listing_id", "url", "checked_at",
            "source_active", "source_price", "source_rooms", "source_area")}
        old["checked_at"] = (module.timestamp(record["checked_at"]) - timedelta(hours=1)).isoformat()
        path.write_text(json.dumps({"schema_version": 1, "records": [old]}), encoding="utf8")
        expected_exception = OSError
        expected_message = "source disk failure"
    original_cache = path.read_bytes()
    original_writer = module.atomic_json
    events = []
    def writer(target, report):
        if failure == "source_disk_failure" and target == path:
            raise OSError("simulated source disk failure")
        if "transaction_status" in report:
            events.append((report["transaction_status"], connection.in_transaction,
                           connection.committed, report.get("source_checks_status")))
        original_writer(target, report)
    monkeypatch.setattr(module, "atomic_json", writer)
    with pytest.raises(expected_exception, match=expected_message):
        await module.run(args)
    assert events == [("prepared", True, False, None),
                      ("committed", False, True, None),
                      ("committed", False, True, "not_published")]
    assert connection.committed is True and connection.closed
    assert connection.rows[record["listing_id"]]["photos"] == record["photos"]
    assert connection.rows[record["listing_id"]]["price"] == record["before"]["price"]
    assert connection.rows[record["listing_id"]]["last_seen"] == module.timestamp(record["before"]["last_seen"])
    receipt = json.loads((tmp_path / "receipt.json").read_text())
    assert receipt["transaction_status"] == "committed" and receipt["summary"] == {"applied": 1}
    assert receipt["source_checks_status"] == "not_published" and expected_message in receipt["source_checks_error"]
    assert receipt["mode"] == "apply" and "operation" not in receipt
    assert path.read_bytes() == original_cache
    assert not path.with_name(path.name + ".lock").exists()


@pytest.mark.asyncio
async def test_apply_receipt_write_failure_rolls_back_transaction(tmp_path, monkeypatch):
    args, record, connection = prepare(tmp_path, monkeypatch, apply=True)
    original_row = deepcopy(connection.rows[record["listing_id"]])
    def fail_write(*_args):
        raise OSError("simulated disk failure")
    monkeypatch.setattr(module, "atomic_json", fail_write)
    with pytest.raises(OSError, match="disk failure"):
        await module.run(args)
    assert connection.rows[record["listing_id"]] == original_row
    assert connection.committed is False
    assert connection.closed


@pytest.mark.asyncio
async def test_sql_failure_rolls_back_and_emits_no_receipt(tmp_path, monkeypatch):
    args, record, connection = prepare(tmp_path, monkeypatch, apply=True)
    connection.fail_update = True
    with pytest.raises(RuntimeError, match="simulated"):
        await module.run(args)
    assert connection.rows[record["listing_id"]]["photos"] is None
    assert not (tmp_path / "receipt.json").exists()
    assert connection.closed


@pytest.mark.asyncio
async def test_stale_source_seed_skip_without_any_mutation(tmp_path, monkeypatch):
    args, record, connection = prepare(tmp_path, monkeypatch, apply=True)
    connection.rows[record["listing_id"]]["last_seen"] += timedelta(minutes=1)
    await module.run(args)
    assert not any(query.startswith("UPDATE") for query, _ in connection.calls)
    receipt = json.loads((tmp_path / "receipt.json").read_text())
    assert receipt["summary"] == {"optimistic_seed_changed": 1}


@pytest.mark.asyncio
async def test_guarded_rollback_refuses_collector_changes(tmp_path, monkeypatch):
    args, record, connection = prepare(tmp_path, monkeypatch, apply=True, rollback=True)
    entry = module.plan_row(record, connection.rows[record["listing_id"]], datetime.now(timezone.utc))
    entry["status"] = "applied"
    connection.rows[record["listing_id"]].update(deepcopy(entry["after"]))
    connection.rows[record["listing_id"]]["last_seen"] += timedelta(minutes=1)
    payload = {"schema_version": 1, "mode": "apply",
               "database_fingerprint": hashlib.sha256(b"mock:local:local").hexdigest(), "rows": [entry]}
    (tmp_path / "manifest.json").write_text(json.dumps(payload), encoding="utf8")
    await module.run(args)
    assert not any(query.startswith("UPDATE") for query, _ in connection.calls)
    receipt = json.loads((tmp_path / "receipt.json").read_text())
    assert receipt["summary"] == {"rollback_state_changed": 1}


@pytest.mark.asyncio
async def test_guarded_rollback_restores_exact_changed_fields(tmp_path, monkeypatch):
    args, record, connection = prepare(tmp_path, monkeypatch, apply=True, rollback=True)
    entry = module.plan_row(record, connection.rows[record["listing_id"]], datetime.now(timezone.utc))
    entry["status"] = "applied"
    connection.rows[record["listing_id"]].update(deepcopy(entry["after"]))
    payload = {"schema_version": 1, "mode": "apply",
               "database_fingerprint": hashlib.sha256(b"mock:local:local").hexdigest(), "rows": [entry]}
    (tmp_path / "manifest.json").write_text(json.dumps(payload), encoding="utf8")
    await module.run(args)
    assert connection.rows[record["listing_id"]]["photos"] is None
    assert connection.rows[record["listing_id"]]["floor"] is None
    assert connection.rows[record["listing_id"]]["price"] == record["before"]["price"]
    receipt = json.loads((tmp_path / "receipt.json").read_text())
    assert receipt["summary"] == {"rolled_back": 1}


@pytest.mark.asyncio
async def test_colliding_receipt_manifest_paths_refused_before_connection(tmp_path, monkeypatch):
    args, record, connection = prepare(tmp_path, monkeypatch, apply=True)
    args.output = args.manifest
    with pytest.raises(ValueError, match="distinct"):
        await module.run(args)
    assert connection.calls == []


@pytest.mark.asyncio
async def test_foreign_database_rollback_receipt_refused_without_update(tmp_path, monkeypatch):
    args, record, connection = prepare(tmp_path, monkeypatch, apply=True, rollback=True)
    (tmp_path / "manifest.json").write_text(json.dumps({"schema_version": 1, "mode": "apply", "database_fingerprint": "wrong", "rows": []}), encoding="utf8")
    with pytest.raises(ValueError, match="does not belong"):
        await module.run(args)
    assert not any(query.startswith("UPDATE") for query, _ in connection.calls)
    assert connection.closed


@pytest.mark.asyncio
async def test_existing_apply_receipt_not_overwritten_and_no_connection(tmp_path, monkeypatch):
    args, record, connection = prepare(tmp_path, monkeypatch, apply=True)
    original = '{"important": "original rollback receipt"}'
    (tmp_path / "receipt.json").write_text(original, encoding="utf8")
    with pytest.raises(ValueError, match="new receipt path"):
        await module.run(args)
    assert not getattr(connection, "connected", False)
    assert connection.calls == []
    assert (tmp_path / "receipt.json").read_text(encoding="utf8") == original


@pytest.mark.asyncio
async def test_source_expiring_while_waiting_for_row_is_rejected(tmp_path, monkeypatch):
    args, record, connection = prepare(tmp_path, monkeypatch, apply=True)
    started_at = module.timestamp(record["checked_at"])
    moments = iter((started_at, started_at + module.MAX_AGE + timedelta(seconds=1)))
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = next(moments)
            return cls.fromisoformat(value.isoformat())
    monkeypatch.setattr(module, "datetime", Clock)
    await module.run(args)
    assert not any(query.startswith("UPDATE") for query, _ in connection.calls)
    receipt = json.loads((tmp_path / "receipt.json").read_text())
    assert receipt["summary"] == {"invalid_manifest_record": 1}


@pytest.mark.parametrize("offset, accepted", [
    (timedelta(hours=-2), True),
    (timedelta(hours=-2, microseconds=-1), False),
    (timedelta(minutes=2), True),
    (timedelta(minutes=2, microseconds=1), False),
])
def test_exact_observation_freshness_and_future_clock_skew_boundaries(offset, accepted):
    record, row = data()
    now = datetime.now(timezone.utc)
    record["checked_at"] = (now + offset).isoformat()
    result = module.plan_row(record, row, now)
    assert result["status"] == ("planned" if accepted else "invalid_manifest_record")
