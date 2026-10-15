"""Monthly index policy tests use only pure rows and a transactional memory fake."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.core import rental_parser as rentals
from bot.db import pg, settings

NOW = datetime(2026, 10, 16, 6, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused:unused@127.0.0.1:1/rental_index_v2_test")
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    reject = AsyncMock(side_effect=AssertionError("No production DB or migrations in index tests"))
    monkeypatch.setattr("asyncpg.connect", reject)
    monkeypatch.setattr("asyncpg.create_pool", reject)
    monkeypatch.setattr(pg, "init_pool", reject)
    monkeypatch.setattr(settings, "get_int", lambda key, default: default)
    # This file tests the stage-0 monthly scope; stage-2 transaction/rebuild
    # behavior has its own independently mocked contract tests.
    monkeypatch.setattr('bot.core.rental_stage2_index.rebuild_rental_stage2_index', AsyncMock())


def source(identifier=0, **changes):
    return {"id": str(identifier), "city": "astana", "district": "Есиль", "complex_name": "Raw name",
            "complex_id": 10, "index_complex_id": 10, "index_complex_name": "Канонический ЖК",
            "complex_binding_verified": True, "rooms": 2, "prop_type": "apartment",
            "rent_period": "monthly", "rent_period_conflict": False, "price": 200000+identifier*10000, "area": 50,
            "title": "2-комнатная, 50 м²", "last_seen": NOW-timedelta(hours=identifier % 10),
            "is_active": True, "archived_at": None, "is_duplicate": False,
            "dup_needs_review": False, "duplicate_of": None, **changes}


def aggregate(**changes):
    return {"median_price": 250000, "avg_price": 250000, "p25_price": 220000,
            "p75_price": 280000, "price_per_sqm": 5000, "sample_count": 10,
            "updated_at": NOW-timedelta(minutes=5), "source_oldest_at": NOW-timedelta(hours=4),
            "source_newest_at": NOW-timedelta(hours=1), "window_days": 30,
            "policy_version": "3", "rent_period": "monthly", **changes}


def records(rows, **kwargs):
    return rentals._rental_index_records(rows, NOW, **kwargs)


def test_exact_inclusive_quartiles_and_source_provenance_for_five_observations():
    result = records([source(i, district=None) for i in range(5)])
    assert len(result) == 2  # ЖК+комнаты, ЖК all rooms; city needs ten.
    for item in result:
        assert item[:4] == ("astana", None, "Канонический ЖК", 10)
        assert item[6:8] == ("monthly", None)
        assert item[8:14] == (220000, 220000, 210000, 230000, 5, 4400)
        assert item[14:] == (NOW, 30, 5, NOW-timedelta(hours=4), NOW, "3")


def test_ten_unbound_rows_contribute_once_per_area_or_city_room_scope():
    rows = [source(i, complex_binding_verified=False, index_complex_id=None) for i in range(10)]
    result = records(rows+rows)
    assert len(result) == 4
    assert all(item[2:4] == (None, None) and item[12] == 10 for item in result)
    assert {item[1] for item in result} == {None, "Есиль"}
    assert {item[4] for item in result} == {None, 2}


def test_missing_district_does_not_multiply_city_sample():
    result = records([source(i, district="", complex_binding_verified=False) for i in range(10)])
    assert len(result) == 2 and all(item[12] == 10 for item in result)


@pytest.mark.parametrize("changes", [
    {"rent_period": "daily"}, {"rent_period": "unknown"}, {"rent_period": None}, {"rent_period_conflict": True},
    {"title": "Сдается посуточно"}, {"is_active": False}, {"archived_at": NOW},
    {"is_duplicate": True}, {"dup_needs_review": True}, {"duplicate_of": "another"},
    {"last_seen": NOW-timedelta(hours=72, seconds=1)}, {"last_seen": NOW+timedelta(seconds=1)},
    {"last_seen": None}, {"last_seen": NOW.replace(tzinfo=None)}, {"city": "almaty"},
    {"prop_type": "parking"}, {"price": 30000}, {"area": float("nan")}, {"rooms": True},
])
def test_one_ineligible_row_cannot_complete_minimum_complex_sample(changes):
    assert records([source(i) for i in range(4)]+[source(4, **changes)]) == []


def test_thresholds_are_applied_separately_and_cannot_be_reduced_below_policy_floor():
    assert records([source(i) for i in range(4)], min_complex=1, min_area=1) == []
    assert len(records([source(i) for i in range(5)])) == 2
    assert len(records([source(i) for i in range(9)])) == 2
    assert len(records([source(i) for i in range(10)])) == 6
    assert records([source(i) for i in range(5)], min_complex=6) == []


def test_latest_duplicate_source_observation_wins():
    rows = [source(i) for i in range(5)]
    rows += [source(0, price=900000, last_seen=NOW-timedelta(hours=2))]
    assert all(item[8] == 220000 and item[12] == 5 for item in records(rows))


def test_canonical_complex_does_not_split_by_raw_name_or_district():
    rows = [source(i, complex_name=f"Вариант {i}", district=f"Район {i}") for i in range(5)]
    result = records(rows)
    assert len(result) == 2 and all(item[1] is None and item[3] == 10 for item in result)


def test_source_binding_sql_prioritizes_fk_and_never_uses_nearest_house():
    sql = rentals._RENTAL_INDEX_ROWS_SQL
    assert "coalesce(bound.cid,named.cid)" in sql
    assert "named ON r.complex_id IS NULL" in sql
    assert "source.cid=identity.id" in sql and "source.cid=identity.parent_complex_id" in sql
    assert "NOT coalesce(identity.is_umbrella,false)" in sql
    assert "HAVING count(DISTINCT cid)=1" in sql and "a.status='verified'" in sql
    assert "r.rent_period = 'monthly'" in sql
    assert all(word not in sql.lower() for word in ("st_distance", "nearest", "r.lat", "r.lon"))


class Context:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *args):
        return False


class Connection:
    def __init__(self, rows, *, fail_delete=False):
        self.rows = rows
        self.calls = []
        self.fail_delete = fail_delete
        self.published = [
            {"city": "astana", "prop_type": "apartment", "rent_period": "monthly", "furnishing": None, "old": True},
            {"city": "astana", "prop_type": "apartment", "rent_period": "unknown", "furnishing": None, "policy_version": None, "old": True},
            {"city": "astana", "prop_type": "apartment", "rent_period": "daily", "furnishing": None},
            {"city": "almaty", "prop_type": "apartment", "rent_period": "monthly", "furnishing": None},
            {"city": "astana", "prop_type": "parking", "rent_period": "monthly", "furnishing": None},
            {"city": "astana", "prop_type": "apartment", "rent_period": "monthly", "furnishing": "full"},
        ]

    def transaction(self):
        connection = self
        class Transaction:
            async def __aenter__(self):
                self.before = deepcopy(connection.published)

            async def __aexit__(self, exc_type, *args):
                if exc_type:
                    connection.published = self.before
        return Transaction()

    async def fetch(self, sql, *args):
        self.calls.append(("fetch", sql, args))
        return self.rows

    async def executemany(self, sql, values):
        self.calls.append(("upsert", sql, values))
        self.published.extend({"record": value} for value in values)

    async def execute(self, sql, *args):
        self.calls.append(("execute", sql, args))
        if "DELETE FROM rental_index" in sql:
            assert "city='astana'" in sql and "prop_type='apartment'" in sql
            assert "rent_period='monthly'" in sql and "furnishing IS NULL" in sql
            if self.fail_delete:
                raise RuntimeError("isolated delete failure")
            self.published = [item for item in self.published if not item.get("old")]


def install_connection(monkeypatch, connection):
    monkeypatch.setattr(pg, "get_pool", lambda: SimpleNamespace(acquire=lambda: Context(connection)))


@pytest.mark.asyncio
async def test_rebuild_removes_out_of_window_groups_in_same_transaction_without_erasing_daily(monkeypatch):
    connection = Connection([])
    install_connection(monkeypatch, connection)
    await rentals.rebuild_rental_index()
    assert len(connection.published) == 4
    assert any(item.get("rent_period") == "daily" for item in connection.published)
    assert [call[0] for call in connection.calls] == ["execute", "fetch", "execute", "execute"]
    assert "pg_advisory_xact_lock" in connection.calls[0][1]
    assert "policy_version IS DISTINCT FROM $2" in connection.calls[2][1]
    assert "rent_period='unknown' AND policy_version IS NULL" in connection.calls[2][1]
    assert connection.calls[2][2][1] == "3"


@pytest.mark.asyncio
async def test_rebuild_upserts_before_scope_cleanup_and_rolls_back_on_failure(monkeypatch):
    # Use timestamps relative to real rebuild time while keeping SQL fully mocked.
    now = datetime.now(timezone.utc)
    connection = Connection([source(i, last_seen=now-timedelta(hours=i)) for i in range(5)], fail_delete=True)
    before = deepcopy(connection.published)
    install_connection(monkeypatch, connection)
    with pytest.raises(RuntimeError, match="isolated delete failure"):
        await rentals.rebuild_rental_index()
    assert connection.published == before
    assert "ON CONFLICT ON CONSTRAINT rental_index_key_v2" in connection.calls[2][1]
    assert connection.calls[2][0] == "upsert" and "DELETE FROM rental_index" in connection.calls[3][1]


@pytest.mark.parametrize("changes", [
    {"policy_version": "2"}, {"policy_version": None}, {"rent_period": "daily"},
    {"window_days": 90}, {"sample_count": 4}, {"sample_count": True},
    {"source_oldest_at": NOW-timedelta(hours=73)}, {"source_oldest_at": None},
    {"source_newest_at": NOW+timedelta(seconds=1)},
    {"source_oldest_at": NOW-timedelta(minutes=10), "source_newest_at": NOW-timedelta(hours=1)},
    {"updated_at": NOW+timedelta(seconds=1)}, {"updated_at": NOW-timedelta(days=4)},
    {"updated_at": NOW-timedelta(hours=2)}, {"p25_price": 260000}, {"p75_price": 200000},
])
def test_reader_rejects_missing_stale_future_reversed_provenance_or_bad_range(changes):
    assert rentals._rental_index_result_ready(aggregate(**changes), NOW, 5) is False


def test_reader_accepts_only_qualified_fresh_provenance_and_scope_minimum():
    assert rentals._rental_index_result_ready(aggregate(), NOW, 10) is True
    assert rentals._rental_index_result_ready(aggregate(sample_count=9), NOW, 10) is False
    assert rentals._rental_index_result_ready(aggregate(sample_count=5), NOW, 5) is True


@pytest.mark.asyncio
async def test_lookup_id_wins_over_wrong_name_and_returns_quartiles_metadata(monkeypatch):
    now = datetime.now(timezone.utc)
    value = aggregate(updated_at=now-timedelta(minutes=1), source_oldest_at=now-timedelta(hours=2),
                      source_newest_at=now-timedelta(hours=1), complex_id=10, complex_name="Канонический ЖК")
    read = AsyncMock(return_value=value)
    monkeypatch.setattr(pg, "fetchrow", read)
    result = await rentals.lookup_rental_estimate("astana", "Есиль", "Неправильное имя", 2, complex_id=10)
    assert result["level"] == "ЖК" and result["complex_id"] == 10
    assert result["p25_price"] == 220000 and result["p75_price"] == 280000
    assert result["updated_at"] == value["updated_at"]
    assert read.await_count == 1
    args = read.await_args.args
    assert args[2] is None and args[6] == 10 and args[7] == 5
    assert "$6::integer IS NULL AND $3::text IS NOT NULL" in args[0]


@pytest.mark.asyncio
async def test_lookup_skips_undersized_complex_and_area_then_preserves_city_room_scope(monkeypatch):
    now = datetime.now(timezone.utc)
    fresh = dict(updated_at=now-timedelta(minutes=1), source_oldest_at=now-timedelta(hours=2),
                 source_newest_at=now-timedelta(hours=1))
    read = AsyncMock(side_effect=[aggregate(sample_count=4, **fresh), aggregate(sample_count=9, **fresh),
                                 aggregate(sample_count=10, **fresh)])
    monkeypatch.setattr(pg, "fetchrow", read)
    result = await rentals.lookup_rental_estimate("astana", "Есиль", "Имя", 2, complex_id=10)
    assert result["level"] == "город"
    assert [call.args[7] for call in read.await_args_list] == [5, 10, 10]
    assert all(call.args[4] == 2 for call in read.await_args_list)
    assert all("i.rent_period='monthly'" in call.args[0] for call in read.await_args_list)


@pytest.mark.asyncio
async def test_lookup_name_fallback_requires_unique_canonical_target_and_verified_index_policy(monkeypatch):
    read = AsyncMock(return_value=None)
    monkeypatch.setattr(pg, "fetchrow", read)
    assert await rentals.lookup_rental_estimate("astana", None, "Старое имя", 2) is None
    sql = read.await_args_list[0].args[0]
    assert "a.status='verified'" in sql and "HAVING count(DISTINCT names.cid)=1" in sql
    assert "NOT coalesce(canonical.is_umbrella,false)" in sql
    assert "i.policy_version=$9" in sql and "i.source_oldest_at BETWEEN" in sql


def test_bad_settings_cannot_relax_minima_or_crash_on_infinity(monkeypatch):
    monkeypatch.setattr(settings, "get_int", lambda key, default: 1)
    assert rentals._rental_index_thresholds() == (5, 10)
    def broken(*args):
        raise OverflowError()
    monkeypatch.setattr(settings, "get_int", broken)
    assert rentals._rental_index_thresholds() == (5, 10)
