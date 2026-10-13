"""No network/database writes are permitted by the source-sample audit tests."""
import json
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from scripts import audit_rental_source_sample as script

NOW = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
URL = "https://krisha.kz/arenda/kvartiry/astana/"


@pytest.fixture(autouse=True)
def no_application_database_or_external_requests(monkeypatch):
    async def reject(*args, **kwargs):
        pytest.fail("Sample audit tests must never open a real database or network")
    monkeypatch.setattr("asyncpg.connect", reject)
    monkeypatch.setattr("asyncpg.create_pool", reject)
    for method in ("init_pool", "execute", "fetch", "fetchrow", "fetchval"):
        monkeypatch.setattr("bot.db.pg." + method, reject)
    monkeypatch.setattr("httpx.AsyncClient.get", reject)


def payload(**overrides):
    return {"url": URL, "checkedAt": "2026-10-13T11:30:00Z", "ids": ["101", "102"], **overrides}


def test_sample_projection_uses_regular_cards_and_discards_contacts_promoted_and_extra_fields():
    sample = script.parse_sample(payload(regularIds=["101", "101", "103"],
        promotedIds=["104"], address="PRIVATE", contacts="PRIVATE", description="PRIVATE"))
    assert sample.ids == ("101", "103")
    assert sample.page == 1 and sample.prop_type == "apartment"
    assert sample.checked_at == datetime(2026, 10, 13, 11, 30, tzinfo=timezone.utc)
    assert "PRIVATE" not in repr(sample)


def test_first_browser_artifact_uses_observed_page_not_last_page_href():
    sample = script.parse_sample({"lastPageHref": "/arenda/kvartiry/astana/?page=207",
                                  "page": 1, "regularIds": ["101"],
                                  "sourceCheckedAt": NOW.isoformat()})
    assert sample.source_url == URL and sample.page == 1


@pytest.mark.parametrize("changes", [
    {"ids": [101]}, {"ids": [True]}, {"ids": [" 101"]}, {"ids": ["101 "]},
    {"ids": ["+101"]}, {"ids": ["101; DROP TABLE rental_listings"]},
    {"ids": ["١٠١"]}, {"ids": ["00101"]}, {"ids": []}, {"ids": ["1" * 21]},
    {"checkedAt": "invalid"}, {"checkedAt": "2026-10-13T12:00:00"},
    {"prop_type": "commercial"}, {"page": 2},
    {"url": "https://user:password@krisha.kz/arenda/kvartiry/astana/"},
    {"url": URL + "?api_key=PRIVATE"}, {"url": URL + "?page=-1"},
    {"url": URL + "?page=0"}, {"url": URL + "?page=10001"},
    {"url": URL + "?page=2&page=3"},
    {"url": URL + "?rent-period-switch=%2Farenda%2Fkvartiry-posutochno"},
    {"url": "https://krisha.kz.evil.test/arenda/kvartiry/astana/"},
    {"url": "http://krisha.kz/arenda/kvartiry/astana/"},
    {"url": "https://krisha.kz:443/arenda/kvartiry/astana/"},
    {"url": URL + "#PRIVATE"}, {"url": "https://krisha.kz/arenda/doma/astana/"},
])
def test_sample_validation_rejects_invalid_ids_metadata_and_unapproved_filter_bounds(changes):
    with pytest.raises(ValueError):
        script.parse_sample(payload(**changes))


def test_last_page_link_without_explicit_observed_page_is_not_a_source_observation():
    with pytest.raises(ValueError, match="explicit observed page"):
        script.parse_sample({"lastPageHref": "/arenda/kvartiry/astana/?page=207",
                             "regularIds": ["101"], "checkedAt": NOW.isoformat()})


def test_load_samples_groups_same_url_time_and_scope_without_double_counting_ids(tmp_path):
    paths = []
    for number, ids in enumerate((["101", "102"], ["102", "103"])):
        path = tmp_path / f"sample{number}.json"
        path.write_text(json.dumps(payload(ids=ids)), encoding="utf-8")
        paths.append(path)
    samples = script.load_samples(paths)
    assert len(samples) == 1 and samples[0].ids == ("101", "102", "103")


def test_file_and_sample_count_limits_are_enforced_before_reading(tmp_path, monkeypatch):
    path = tmp_path / "sample.json"
    path.write_text(json.dumps(payload()), encoding="utf-8")
    monkeypatch.setattr(script, "MAX_FILE_BYTES", 2)
    with pytest.raises(ValueError, match="too large"):
        script.load_samples([path])
    with pytest.raises(ValueError, match="bounded source sample"):
        script.load_samples([path] * (script.MAX_SAMPLES + 1))


def counts(size, **changes):
    return {name: size for name in script.COUNT_FIELDS} | changes


def test_count_projection_exports_only_approved_aggregates():
    row = counts(2, db_present=1, correct_scope=1, active_clean=1,
                 fresh_72h=1, fresh_1h=0, with_stored_price=1, with_stored_cover=0)
    result = script.count_projection({**row, "address": "PRIVATE", "ids": ["PRIVATE"]}, 2)
    assert result["presence_percent"] == 50 and result["missing_from_database"] == 1
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize("changes", [
    {"sample_size": 3}, {"db_present": -1}, {"db_present": 3}, {"db_present": True},
    {"db_present": 0}, {"active_clean": 0}, {"fresh_72h": 0}, {"with_stored_price": "2"},
])
def test_invalid_aggregate_counts_fail_closed(changes):
    with pytest.raises(RuntimeError):
        script.count_projection(counts(2, **changes), 2)


class FakeConnection:
    def __init__(self, readonly="on"):
        self.readonly = readonly
        self.queries = []
        self.closed = False

    def transaction(self, **options):
        self.options = options
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def fetchval(self, query):
        return self.readonly if query.startswith("SHOW") else NOW

    async def fetchrow(self, query, ids, kind, at):
        self.queries.append((query, ids, kind, at))
        assert "last_seen BETWEEN" in query
        assert "dup_needs_review" in query and "duplicate_of" in query
        assert "archived_at IS NULL" in query and "city = 'astana'" in query
        return counts(len(ids)) | {"description": "PRIVATE", "price": "PRIVATE"}

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("readonly", ["on", "off"])
async def test_audit_requires_raw_readonly_connection_and_transaction_before_selects(monkeypatch, readonly):
    connection = FakeConnection(readonly)
    options = {}

    async def connect(dsn, **kwargs):
        options.update(kwargs)
        return connection

    monkeypatch.setitem(sys.modules, "asyncpg", SimpleNamespace(connect=connect))
    samples = [script.parse_sample(payload()), script.parse_sample(payload(
        url="https://krisha.kz/arenda/garazhi/astana/", ids=["102", "103"]))]
    if readonly == "off":
        with pytest.raises(RuntimeError, match="read-only"):
            await script.audit("postgresql://PRIVATE", samples)
        assert connection.queries == []
    else:
        report = await script.audit("postgresql://PRIVATE", samples)
        assert report["combined"]["unique_source_ids"] == 3
        assert report["combined"]["id_observations"] == 4
        assert report["market_completeness"] == "unknown_convenience_sample_only"
        assert report["combined"]["presence_percent"] == 100
        assert "not proof" in report["note"]
        assert report["database_writes"] == 0 and len(connection.queries) == 3
        assert "PRIVATE" not in json.dumps(report)
        assert all(query.strip().startswith(("SELECT", "WITH")) for query, *_ in connection.queries)
    assert options["server_settings"]["default_transaction_read_only"] == "on"
    assert connection.options == {"readonly": True, "isolation": "repeatable_read"}
    assert connection.closed


def test_cli_does_not_print_credentials_from_database_failure(tmp_path, monkeypatch, capsys):
    path = tmp_path / "sample.json"
    path.write_text(json.dumps(payload()), encoding="utf-8")
    monkeypatch.setenv("DATABASE_URL", "postgresql://PRIVATE_PASSWORD")
    monkeypatch.setattr(sys, "argv", ["audit", "--root", str(tmp_path), "--sample", str(path)])

    async def failed(*args):
        raise RuntimeError("PRIVATE_PASSWORD")

    monkeypatch.setattr(script, "audit", failed)
    assert script.main() == 1
    captured = capsys.readouterr()
    assert "RuntimeError" in captured.err and "PRIVATE_PASSWORD" not in captured.err
    assert captured.out == ""
