"""Safety invariants for reviewed redirects; never connect to production."""
from __future__ import annotations
from copy import deepcopy
import json
import os
from urllib.parse import urlsplit

import pytest

from bot.core import complex_identity_review as review


def complexes():
    return [
        {"id": 1, "name": "Tumar Club", "address": "Анет баба, 4", "lat": 51.13, "lon": 71.38,
         "developer_id": 7, "parent_complex_id": None, "is_umbrella": False,
         "housing_class": "business", "is_newbuild": True, "krisha_url": None,
         "korter_url": None, "canonical_id": None, "canonical_reason": None,
         "is_garbage": False, "is_street": False},
        {"id": 2, "name": "Тумар Клаб", "address": "Анет баба, 4", "lat": 51.1301, "lon": 71.38,
         "developer_id": 7, "parent_complex_id": None, "is_umbrella": False,
         "housing_class": "business", "is_newbuild": True, "krisha_url": None,
         "korter_url": None, "canonical_id": None, "canonical_reason": None,
         "is_garbage": False, "is_street": False},
    ]


def decision(rows=None, *, relations=(), overrides=(), status="review"):
    return review.build_decision(
        {"id": 1, "complex_id_a": 1, "complex_id_b": 2, "status": status},
        rows or complexes(), {1: 10, 2: 3}, list(relations), list(overrides))


def test_review_decision_stable_id_and_source_values_preserved():
    rows = complexes()
    original = deepcopy(rows)
    result = decision(rows)
    assert result["blockers"] == []
    assert (result["canon_id"], result["dup_id"]) == (1, 2)
    assert result["canonical_changes"] == [{
        "complex_id": 2, "previous_canonical_id": None, "previous_reason": None,
        "canonical_id": 1, "reason": "manual_review",
    }]
    assert rows == original


@pytest.mark.parametrize("change,expected", [
    ({"name": "Tumar Club 2 очередь"}, "номера очереди"),
    ({"name": "Tumar Club Gold"}, "названия продукта"),
    ({"parent_complex_id": 99}, "родительские"),
    ({"developer_id": 88}, "застройщики"),
    ({"lat": 51.15}, "300 м"),
    ({"address": "Туркестан, 4", "developer_id": None}, "независимых"),
    ({"is_garbage": True}, "неподтверждённая"),
])
def test_conflicting_record_cannot_be_merged(change, expected):
    rows = complexes()
    rows[1].update(change)
    assert any(expected in value for value in decision(rows)["blockers"])


@pytest.mark.parametrize("kind", ["sibling_phase", "same_umbrella_project", "separate_neighbor_complex"])
def test_reviewed_separation_blocks_redirect(kind):
    result = decision(relations=[{"complex_id_a": 1, "complex_id_b": 2, "relation_type": kind}])
    assert any("противоречит" in value for value in result["blockers"])


def test_two_existing_manual_roots_cannot_be_changed():
    result = decision(overrides=[{"complex_id": 1, "canonical_id": None},
                                 {"complex_id": 2, "canonical_id": None}])
    assert any("закреплена" in value for value in result["blockers"])


@pytest.mark.parametrize("status", ["merged", "rejected"])
def test_resolved_candidate_never_reopens_as_a_merge(status):
    assert any("уже рассмотрен" in value for value in decision(status=status)["blockers"])


def test_transitive_separation_still_blocks_candidate():
    rows = complexes() + [{**complexes()[1], "id": 3, "canonical_id": 2, "canonical_reason": "krisha_slug"}]
    result = decision(rows, relations=[
        {"complex_id_a": 1, "complex_id_b": 3, "relation_type": "separate_neighbor_complex"}
    ])
    # Existing canonical alias 3 would be moved onto 1 by 2 -> 1; the resolver
    # protects reviewed separation across whole identity components.
    assert result["blockers"]


def test_preview_is_select_only_and_uses_exact_resolver_scoped_to_family():
    sql = review.build_shadow_preview_sql()
    assert "UPDATE " not in sql and "INSERT " not in sql and "DELETE " not in sql
    assert "FROM reviewed_complexes" in sql
    assert "FROM reviewed_listings" in sql
    assert "SELECT * FROM apartment_listings WHERE id=ANY($1::text[])" in sql
    assert "jsonb_to_recordset($2::jsonb)" in sql


@pytest.mark.parametrize("fingerprint,note,urls,code", [
    ("", "Достаточное объяснение физической идентичности", ["https://korter.kz/project"], "preview_required"),
    ("a"*64, "", ["https://korter.kz/project"], "evidence_required"),
    ("a"*64, "Общий адрес подтверждён источником", [], "evidence_required"),
    ("a"*64, "Общий адрес подтверждён источником", ["http://korter.kz/project"], "evidence_required"),
    ("a"*64, "Общий адрес подтверждён источником", ["https://user:pass@korter.kz/project"], "evidence_required"),
])
def test_approval_requires_fresh_preview_and_source_evidence(fingerprint, note, urls, code):
    with pytest.raises(review.ComplexIdentityReviewError) as error:
        review.validate_review(fingerprint, note, urls, "admin")
    assert error.value.code == code


def test_discovery_preserves_numbers_and_never_reopens_resolved_pairs():
    rows = complexes()
    rows[0]["name"] = "Tumar Club"
    rows[1]["name"] = 'ЖК "Tumar Club"'
    rows += [{**rows[0], "id": 3, "name": "Tumar Club 2 очередь"},
             {**rows[0], "id": 4, "canonical_id": 1}]
    result = review.discover_review_pairs(rows, {1: 10, 2: 3})
    assert len(result) == 1
    assert result[0]["evidence"]["canonical_decision_made"] is False
    assert result[0]["evidence"]["source_urls_verified"] is False
    assert review.discover_review_pairs(rows, {}, existing_pairs=[(2, 1)]) == []


def test_discovery_keeps_geo_conflict_as_unconfirmed_review():
    rows = complexes()
    rows[1]["name"] = "Tumar Club"
    rows[1]["lat"] = 51.22
    result = review.discover_review_pairs(rows, {})
    assert result[0]["evidence"]["geo_m"] > 300
    assert result[0]["source_verified"] is False
    assert decision(rows)["blockers"]


class RecordingConnection:
    def __init__(self):
        self.executed = []

    async def fetchval(self, sql, *args):
        return 1

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "UPDATE 1"

    async def fetchrow(self, sql, *args):
        return None


def preview():
    return {
        "candidate_id": 7, "fingerprint": "a"*64, "can_apply": True, "blockers": [],
        "canon_id": 1, "dup_id": 2, "complexes": complexes(),
        "canonical_changes": decision()["canonical_changes"],
        "listing_changes": {"count": 1, "rebound": 1, "detached": 0, "method_only": 0, "samples": []},
        "preserved": {"source_links": 2, "newbuild_units": 3, "properties": 4, "favorites": 1},
        "rollback_guard": {"component_ids": [1,2], "listings_hash": "test", "complexes_hash": "test",
                           "preserved_counts": {"source_links": 2, "newbuild_units": 3, "properties": 4, "favorites": 1}},
    }


@pytest.mark.asyncio
async def test_stale_preview_does_not_write_identity(monkeypatch):
    async def snapshot(*args):
        return preview(), []
    monkeypatch.setattr(review, "_snapshot", snapshot)
    conn = RecordingConnection()
    with pytest.raises(review.ComplexIdentityReviewError) as error:
        await review._apply(conn, 7, "b"*64, "Один адрес проверен по источнику", ["https://korter.kz/tumar"], "admin")
    assert error.value.code == "stale_preview"
    assert all("UPDATE " not in sql and "INSERT " not in sql for sql, _ in conn.executed)


def test_rollback_guard_tracks_new_membership_and_exact_preserved_identity():
    affected = [{"id":"original","complex_id":2,"complex_resolution":"name",
                 "complex_name":"Тумар Клаб","complex_url":None,"resolved_house_id":None}]
    changed = [{"listing_id":"original","complex_id":1,"method":"name"}]
    canonical = decision()["canonical_changes"]
    guard = review.build_rollback_guard(affected, complexes(), changed, canonical, [1,2], {},
                                        preserved_identity_hash="identity-proof")
    expected = [{**affected[0],"complex_id":1}]
    assert guard["listings_hash"] == review._digest(expected)
    assert guard["listings_hash"] != review._digest(expected + [{**expected[0],"id":"new-arrival"}])
    assert guard["preserved_identity_hash"] == "identity-proof"
    assert affected[0]["complex_id"] == 2


def test_preserved_identity_hash_detects_a_foreign_key_move_without_count_change():
    before = [{"kind":"newbuild_units","identity_id":"31","complex_id":2,"key_a":"developer","key_b":"unit"}]
    after = [{**before[0],"complex_id":1}]
    assert len(before) == len(after)
    assert review._digest(before) != review._digest(after)

@pytest.mark.asyncio
async def test_apply_preserves_source_spine_text_and_property_unit_favorite_identity(monkeypatch):
    change = {"listing_id": "source-id", "previous_complex_id": 2,
              "previous_complex_resolution": "name", "complex_id": 1, "method": "name",
              "evidence": {"complex_name": "Тумар Клаб"}}
    async def snapshot(*args):
        return preview(), [change]
    monkeypatch.setattr(review, "_snapshot", snapshot)
    conn = RecordingConnection()
    result = await review._apply(conn, 7, "a"*64, "Один адрес проверен по источнику",
                                 ["https://korter.kz/tumar"], "admin")
    statements = [sql for sql, _ in conn.executed]
    assert result["applied"] and result["links_moved"] == 0 and result["favorites_moved"] == 0
    assert any("INSERT INTO listing_complex_resolution_log" in sql for sql in statements)
    for target in ("complex_source_links", "properties", "property_listings", "newbuild_units", "complex_favorites"):
        assert not any(f"UPDATE {target} " in sql or f"DELETE FROM {target} " in sql for sql in statements)
    assert not any("SET complex_name" in sql or "SET is_garbage" in sql for sql in statements)
    event_args = next(args for sql, args in conn.executed if "INSERT INTO complex_canonical_override_log" in sql)
    evidence = json.loads(event_args[2])
    assert evidence["listing_changes"][0]["previous_complex_id"] == 2
    assert evidence["canonical_changes"][0]["previous_canonical_id"] is None


@pytest.mark.asyncio
async def test_blocked_preview_does_not_write_identity(monkeypatch):
    blocked = preview()
    blocked["can_apply"] = False
    blocked["blockers"] = ["Разные очереди"]
    async def snapshot(*args):
        return blocked, []
    monkeypatch.setattr(review, "_snapshot", snapshot)
    conn = RecordingConnection()
    with pytest.raises(review.ComplexIdentityReviewError) as error:
        await review._apply(conn, 7, "a"*64, "Один адрес проверен по источнику", ["https://korter.kz/tumar"], "admin")
    assert error.value.code == "identity_conflict"
    assert all("UPDATE " not in sql and "INSERT " not in sql for sql, _ in conn.executed)


@pytest.mark.asyncio
async def test_guarded_rollback_refuses_changed_override():
    class Changed(RecordingConnection):
        async def fetchrow(self, sql, *args):
            if "complex_canonical_override_log" in sql:
                return {"complex_id": 2, "canonical_id": 1, "evidence": {"review_id": "test"}}
            return {"canonical_id": 99, "evidence": {"review_id": "another"}}
    conn = Changed()
    with pytest.raises(review.ComplexIdentityReviewError) as error:
        await review._rollback(conn, "test", "admin")
    assert error.value.code == "rollback_conflict"
    assert all("UPDATE " not in sql and "DELETE " not in sql and "INSERT " not in sql for sql, _ in conn.executed)


def test_discovery_prioritizes_impact_and_limits_work():
    rows = []
    for index in range(25):
        one = {**complexes()[0], "id": index * 2 + 1, "name": "Проект" + chr(65 + index)}
        two = {**one, "id": index * 2 + 2, "name": 'ЖК "' + one["name"] + '"'}
        rows.extend((one, two))
    counts = {row["id"]: row["id"] for row in rows}
    result = review.discover_review_pairs(rows, counts, limit=20)
    assert len(result) == 20
    assert result[0]["impact_listings"] > result[-1]["impact_listings"]
    assert all(row["complex_id_a"] < row["complex_id_b"] for row in result)


def test_discovery_never_groups_different_product_or_phase():
    rows = complexes()
    rows[0]["name"] = "Nova City Gold 2 очередь"
    rows[1]["name"] = "Nova City Comfort 1 очередь"
    assert review.discover_review_pairs(rows, {}) == []


class Transaction:
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return False


@pytest.mark.asyncio
async def test_nomination_only_records_candidate_and_never_reopens_resolved_pair():
    class Nomination(RecordingConnection):
        def transaction(self, **kwargs):
            return Transaction()
        async def fetch(self, sql, *args):
            return complexes()
        async def fetchrow(self, sql, *args):
            self.executed.append((sql, args))
            if "INSERT" in sql:
                return None
            return {"id": 33, "status": "rejected"}
    conn = Nomination()
    result = await review.nominate_complex_pair(1, 2, review_note="Проверить два написания одного проекта",
                                               connection=conn)
    assert result == {"candidate_id": 33, "status": "rejected", "created": False}
    statements = [sql for sql, _ in conn.executed]
    assert any("INSERT INTO complex_duplicate_candidates" in sql for sql in statements)
    assert not any("UPDATE " in sql or "DELETE " in sql for sql in statements)
    assert "DO NOTHING" in statements[0]


@pytest.mark.asyncio
async def test_refresh_preview_uses_readonly_transaction_and_never_initializes_pool(monkeypatch):
    from scripts import refresh_complex_identity_review as refresh
    class Readonly:
        def __init__(self):
            self.transactions = []
            self.closed = False
        def transaction(self, **kwargs):
            self.transactions.append(kwargs)
            return Transaction()
        async def fetch(self, sql, *args):
            if "FROM complexes" in sql:
                rows = complexes()
                rows[1]["name"] = 'ЖК "Tumar Club"'
                return rows
            if "count(*)" in sql:
                return [{"complex_id": 1, "n": 10}, {"complex_id": 2, "n": 1}]
            return []
        async def close(self):
            self.closed = True
    conn = Readonly()
    async def connect(*args, **kwargs):
        return conn
    monkeypatch.setattr(refresh.asyncpg, "connect", connect)
    result = await refresh.refresh("unused", publish=False)
    assert result["read_only"] is True
    assert result["identity_writes"] == 0
    assert result["inserted_candidate_ids"] == []
    assert len(result["candidates"]) == 1
    assert conn.transactions == [{"isolation": "repeatable_read", "readonly": True}]
    assert conn.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_method", ["merge_complex_pair", "approve_duplicate_candidate"])
async def test_legacy_merge_functions_deny_before_sql(monkeypatch, legacy_method):
    from bot.core import entity_resolution
    from bot.db import pg
    async def forbidden(*args, **kwargs):
        pytest.fail("A legacy merge attempted database access")
    for name in ("fetch", "fetchrow", "fetchval", "execute"):
        monkeypatch.setattr(pg, name, forbidden)
    with pytest.raises(review.ComplexIdentityReviewError) as error:
        if legacy_method == "merge_complex_pair":
            await entity_resolution.merge_complex_pair(2, 1, method="old_cli", matched_by="auto")
        else:
            await entity_resolution.approve_duplicate_candidate(7)
    assert error.value.code == "preview_required"


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [review.asyncpg.SerializationError, review.asyncpg.DeadlockDetectedError])
async def test_serializable_commit_conflict_becomes_refreshable_preview_error(error_type):
    class ConflictTransaction(Transaction):
        async def __aexit__(self, *args):
            raise error_type("Concurrent operation")
    class ConflictConnection:
        def transaction(self, **kwargs):
            assert kwargs["isolation"] == "serializable"
            return ConflictTransaction()
    async def completed(*args):
        return {"applied": True}
    with pytest.raises(review.ComplexIdentityReviewError) as error:
        await review._serializable_action(ConflictConnection(), completed)
    assert error.value.code == "stale_preview"

