"""tests/test_property_merge_provenance.py — durable auditability/
provenance layer (bot/identity/property_merge_provenance.py,
scripts/property_merge_batch_runner.py, migrations/093). Synthetic
fixtures only (тот же паттерн, что tests/test_property_merge.py) —
реальная Postgres test DB (DATABASE_URL), id с префиксом
'__test_pmprov_...__', удаляются в finally."""
import importlib.util
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

_BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
_CLEAN_PROVENANCE = {"git_sha": "deadbeef" * 5, "git_branch": "master", "git_dirty": False}


def _dt(days: float) -> datetime:
    return _BASE + timedelta(days=days)


@pytest_asyncio.fixture
async def db():
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    yield
    await close_pool()


def _load_batch_runner_module():
    path = os.path.join(os.path.dirname(__file__), "..", "scripts", "property_merge_batch_runner.py")
    spec = importlib.util.spec_from_file_location("property_merge_batch_runner_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── synthetic fixture helpers (тот же паттерн, что test_property_merge.py) ──

async def _insert_listing(lid, *, address="Тест пров, 1", floor=5, area=45.0, rooms=2, price=20000000,
                           first_seen=None, last_seen=None):
    from bot.db.pg import execute
    await execute(
        """
        INSERT INTO apartment_listings (id, url, address, floor, area, rooms, price, is_active, first_seen, last_seen)
        VALUES ($1,$2,$3,$4,$5,$6,$7,TRUE,$8,$9)
        ON CONFLICT (id) DO UPDATE SET address=$3, floor=$4, area=$5, rooms=$6, price=$7, first_seen=$8, last_seen=$9
        """,
        lid, f"https://krisha.kz/test/{lid}", address, floor, area, rooms, price, first_seen, last_seen,
    )


async def _make_property(address_hash, *, floor=5, area_sqm=45.0, rooms=2, first_seen_at=None, last_seen_at=None):
    from bot.db.pg import fetchval
    return await fetchval(
        "INSERT INTO properties (address_hash, floor, area_sqm, rooms, identity_status, first_seen_at, last_seen_at) "
        "VALUES ($1,$2,$3,$4,'provisional', COALESCE($5, now()), COALESCE($6, now())) RETURNING property_id",
        address_hash, floor, area_sqm, rooms, first_seen_at, last_seen_at,
    )


async def _link(property_id, listing_id):
    from bot.db.pg import execute
    await execute(
        "INSERT INTO property_listings (property_id, listing_id, link_method) VALUES ($1,$2,'bootstrap')",
        property_id, listing_id,
    )


async def _make_candidate(listing_id, candidate_property_id, *, relationship_type="relist", status="pending"):
    from bot.db.pg import fetchval
    return await fetchval(
        """
        INSERT INTO property_match_candidates
            (listing_id, candidate_property_id, match_method, match_score, relationship_type, matcher_version, status)
        VALUES ($1,$2,'exact_hash',0.9,$3,'test_prov_v1',$4)
        RETURNING candidate_id
        """,
        listing_id, candidate_property_id, relationship_type, status,
    )


async def _accept(candidate_id, *, reviewed_by="pytest"):
    from bot.db.pg import execute, fetchrow
    c = await fetchrow(
        "SELECT candidate_id, listing_id, candidate_property_id, evidence, matcher_version "
        "FROM property_match_candidates WHERE candidate_id=$1", candidate_id)
    await execute(
        "UPDATE property_match_candidates SET status='accepted', reviewed_at=now(), reviewed_by=$2 "
        "WHERE candidate_id=$1", candidate_id, reviewed_by)
    await execute(
        """
        INSERT INTO property_match_review_log
            (candidate_id, listing_id, candidate_property_id, decision, matcher_version, evidence_snapshot, reviewed_by)
        VALUES ($1,$2,$3,'accepted',$4,$5,$6)
        """,
        c["candidate_id"], c["listing_id"], c["candidate_property_id"], c["matcher_version"], c["evidence"], reviewed_by,
    )


async def _make_pair_and_plan(suffix: str):
    """Один accepted pair (pa, pb) + свежий plan_property_merge() manifest
    для него. Возвращает (manifest, la, lb, pa, pb)."""
    from bot.identity.property_merge import plan_property_merge

    la, lb = f"__test_pmprov_{suffix}_a__", f"__test_pmprov_{suffix}_b__"
    await _insert_listing(la, address=f"Пров {suffix}, 1", first_seen=_dt(0), last_seen=_dt(5))
    await _insert_listing(lb, address=f"Пров {suffix}, 1", first_seen=_dt(10), last_seen=_dt(15))
    pa = await _make_property(f"__test_pmprov_hash_{suffix}_a__", first_seen_at=_dt(0), last_seen_at=_dt(5))
    pb = await _make_property(f"__test_pmprov_hash_{suffix}_b__", first_seen_at=_dt(10), last_seen_at=_dt(15))
    await _link(pa, la)
    await _link(pb, lb)
    cid = await _make_candidate(la, pb)
    await _accept(cid)

    plans = await plan_property_merge({pa, pb})
    plan = next(p for p in plans if pa in p["members"])
    assert plan["status"] == "planned", plan
    return plan["manifest"], la, lb, pa, pb


async def _cleanup(listing_ids, property_ids):
    from bot.db.pg import execute
    lids, pids = list(listing_ids), list(property_ids)
    # Новые audit-таблицы — ПЕРЕД property_merge_log/properties (FK).
    await execute(
        "DELETE FROM property_merge_validation_log WHERE canonical_property_id = ANY($1::int[])", pids)
    await execute(
        "DELETE FROM property_merge_execution_log WHERE manifest_id IN "
        "(SELECT manifest_id FROM property_merge_manifest_log WHERE canonical_property_id = ANY($1::int[]))", pids)
    await execute(
        "DELETE FROM property_merge_manifest_log WHERE canonical_property_id = ANY($1::int[])", pids)
    await execute(
        "DELETE FROM property_merge_provenance_note WHERE merge_group_key IN "
        "(SELECT merge_group_key FROM property_merge_log WHERE canonical_property_id = ANY($1::int[]) "
        " OR losing_property_id = ANY($1::int[]))", pids)
    await execute(
        "DELETE FROM property_merge_log WHERE canonical_property_id = ANY($1::int[]) "
        "OR losing_property_id = ANY($1::int[])", pids)
    await execute(
        "DELETE FROM property_match_review_log WHERE candidate_property_id = ANY($1::int[]) "
        "OR listing_id = ANY($2::text[])", pids, lids)
    await execute(
        "DELETE FROM property_match_candidates WHERE listing_id = ANY($1::text[]) "
        "OR candidate_property_id = ANY($2::int[])", lids, pids)
    await execute("DELETE FROM price_history WHERE listing_id = ANY($1::text[])", lids)
    await execute("DELETE FROM listing_archive_history WHERE listing_id = ANY($1::text[])", lids)
    await execute("DELETE FROM property_listings WHERE listing_id = ANY($1::text[])", lids)
    await execute("DELETE FROM properties WHERE property_id = ANY($1::int[])", pids)
    await execute("DELETE FROM apartment_listings WHERE id = ANY($1::text[])", lids)


# ── 1+2. manifest persisted before apply, apply result persisted ────────────

@pytest.mark.asyncio
async def test_manifest_and_execution_persisted_on_successful_apply(db):
    from bot.identity.property_merge_provenance import apply_property_merge_durable
    from bot.db.pg import fetchrow

    manifest, la, lb, pa, pb = await _make_pair_and_plan("persist")
    try:
        result = await apply_property_merge_durable(
            manifest, actor="pytest", dry_run=False, git_provenance=_CLEAN_PROVENANCE,
        )
        assert result["status"] == "merged"
        manifest_id, execution_id = result["manifest_id"], result["execution_id"]

        m_row = await fetchrow(
            "SELECT component_hash, canonical_property_id, actor, git_branch, git_dirty "
            "FROM property_merge_manifest_log WHERE manifest_id=$1", manifest_id)
        assert m_row is not None
        assert m_row["component_hash"] == manifest["component_hash"]
        assert m_row["canonical_property_id"] == result["canonical_property_id"]
        assert m_row["actor"] == "pytest"
        assert m_row["git_branch"] == "master"
        assert m_row["git_dirty"] is False

        e_row = await fetchrow(
            "SELECT status, manifest_id, merge_group_key, rows_repointed, started_at, finished_at "
            "FROM property_merge_execution_log WHERE execution_id=$1", execution_id)
        assert e_row is not None
        assert e_row["status"] == "merged"
        assert e_row["manifest_id"] == manifest_id
        assert str(e_row["merge_group_key"]) == result["merge_group_key"]
        rows_repointed = json.loads(e_row["rows_repointed"])
        assert len(rows_repointed) == 1
        assert rows_repointed[0]["listing_id"] in (la, lb)
        assert e_row["started_at"] <= e_row["finished_at"]
    finally:
        await _cleanup([la, lb], [pa, pb])


# ── 3. validation result persisted ───────────────────────────────────────

@pytest.mark.asyncio
async def test_validation_result_persisted(db):
    from bot.identity.property_merge_provenance import apply_property_merge_durable, validate_property_merge
    from bot.db.pg import fetchrow

    manifest, la, lb, pa, pb = await _make_pair_and_plan("validate")
    try:
        result = await apply_property_merge_durable(manifest, actor="pytest", dry_run=False,
                                                      git_provenance=_CLEAN_PROVENANCE)
        assert result["status"] == "merged"

        validation = await validate_property_merge(result["execution_id"])
        assert validation["passed"] is True, validation["checks"]
        names = {c["name"] for c in validation["checks"]}
        assert "expected_listing_ids_present_on_canonical" in names
        assert "timeline_events_deterministic" in names
        assert "candidate_statuses_untouched" in names

        v_row = await fetchrow(
            "SELECT execution_id, canonical_property_id, passed FROM property_merge_validation_log "
            "WHERE validation_id=$1", validation["validation_id"])
        assert v_row is not None
        assert v_row["execution_id"] == result["execution_id"]
        assert v_row["passed"] is True
    finally:
        await _cleanup([la, lb], [pa, pb])


# ── 4. batch stops on component #2 failure, #3 не применяется ──────────────

@pytest.mark.asyncio
async def test_batch_stops_on_component_2_failure_component_3_not_applied(db):
    from bot.identity.property_merge import save_manifest
    from bot.db.pg import execute, fetchval, fetchrow

    manifest_a, la1, la2, pa1, pa2 = await _make_pair_and_plan("batch4a")
    manifest_b, lb1, lb2, pb1, pb2 = await _make_pair_and_plan("batch4b")
    manifest_c, lc1, lc2, pc1, pc2 = await _make_pair_and_plan("batch4c")
    all_lids = [la1, la2, lb1, lb2, lc1, lc2]
    all_pids = [pa1, pa2, pb1, pb2, pc1, pc2]

    # Компонент B становится stale ПОСЛЕ построения manifest'а — тот же
    # сценарий, что test_stale_manifest_no_apply, здесь используется как
    # инструмент, чтобы batch дошёл до #2 и остановился на нём.
    await execute("UPDATE property_match_candidates SET status='rejected' WHERE candidate_id = ANY($1::int[])",
                  manifest_b["candidate_ids"])

    tmpdir = tempfile.mkdtemp()
    paths = {}
    for name, m in (("a", manifest_a), ("b", manifest_b), ("c", manifest_c)):
        p = os.path.join(tmpdir, f"{name}.json")
        save_manifest(m, p)
        paths[name] = p

    mod = _load_batch_runner_module()
    items = [
        {"manifest_path": paths["a"]},
        {"manifest_path": paths["b"]},
        {"manifest_path": paths["c"]},
    ]
    try:
        rc = await mod._run(items, actor="pytest-batch", apply=True, allow_non_master=False, override_reason=None,
                             git_provenance=_CLEAN_PROVENANCE)
        assert rc == 1

        # A применился.
        assert await fetchval("SELECT property_id FROM property_listings WHERE listing_id=$1", la1) == \
               await fetchval("SELECT property_id FROM property_listings WHERE listing_id=$1", la2)

        # B заблокирован как stale, НЕ применён.
        assert await fetchval("SELECT property_id FROM property_listings WHERE listing_id=$1", lb1) == pb1
        assert await fetchval("SELECT property_id FROM property_listings WHERE listing_id=$1", lb2) == pb2
        exec_b = await fetchrow(
            "SELECT status FROM property_merge_execution_log WHERE manifest_id = "
            "(SELECT manifest_id FROM property_merge_manifest_log WHERE canonical_property_id = ANY($1::int[]) "
            " ORDER BY manifest_id DESC LIMIT 1)", [pb1, pb2])
        assert exec_b is not None and exec_b["status"] == "blocked_stale"

        # C — batch остановился ДО него: property_listings нетронуты, и
        # НИ ОДНОЙ manifest_log-строки для C не появилось (persist_manifest
        # для C ни разу не вызывался — batch стоял на #2).
        assert await fetchval("SELECT property_id FROM property_listings WHERE listing_id=$1", lc1) == pc1
        assert await fetchval("SELECT property_id FROM property_listings WHERE listing_id=$1", lc2) == pc2
        n_manifest_c = await fetchval(
            "SELECT count(*) FROM property_merge_manifest_log WHERE canonical_property_id = ANY($1::int[])",
            [pc1, pc2])
        assert n_manifest_c == 0
    finally:
        await _cleanup(all_lids, all_pids)


# ── 5. batch stops on validation failure ─────────────────────────────────

@pytest.mark.asyncio
async def test_batch_stops_on_validation_failure(db):
    from bot.identity.property_merge import save_manifest
    import bot.identity.property_merge_provenance as provenance_module
    from bot.db.pg import fetchval

    manifest_a, la1, la2, pa1, pa2 = await _make_pair_and_plan("batch5a")
    manifest_b, lb1, lb2, pb1, pb2 = await _make_pair_and_plan("batch5b")
    manifest_c, lc1, lc2, pc1, pc2 = await _make_pair_and_plan("batch5c")
    all_lids = [la1, la2, lb1, lb2, lc1, lc2]
    all_pids = [pa1, pa2, pb1, pb2, pc1, pc2]

    canonical_b = manifest_b["canonical_property_id"]
    real_validate = provenance_module.validate_property_merge

    async def fake_validate(execution_id):
        res = await real_validate(execution_id)
        if res["canonical_property_id"] == canonical_b:
            return {**res, "passed": False}
        return res

    tmpdir = tempfile.mkdtemp()
    paths = {}
    for name, m in (("a", manifest_a), ("b", manifest_b), ("c", manifest_c)):
        p = os.path.join(tmpdir, f"{name}.json")
        save_manifest(m, p)
        paths[name] = p

    mod = _load_batch_runner_module()
    items = [{"manifest_path": paths["a"]}, {"manifest_path": paths["b"]}, {"manifest_path": paths["c"]}]
    original = provenance_module.validate_property_merge
    provenance_module.validate_property_merge = fake_validate
    try:
        rc = await mod._run(items, actor="pytest-batch5", apply=True, allow_non_master=False, override_reason=None,
                             git_provenance=_CLEAN_PROVENANCE)
        assert rc == 1

        # A: применён И validated успешно.
        assert await fetchval("SELECT property_id FROM property_listings WHERE listing_id=$1", la1) == \
               await fetchval("SELECT property_id FROM property_listings WHERE listing_id=$1", la2)
        # B: apply прошёл (merged), но validation "провалилась" -> batch стоп.
        assert await fetchval("SELECT property_id FROM property_listings WHERE listing_id=$1", lb1) == \
               await fetchval("SELECT property_id FROM property_listings WHERE listing_id=$1", lb2)
        # C: НЕ применён вообще (batch остановился на B).
        assert await fetchval("SELECT property_id FROM property_listings WHERE listing_id=$1", lc1) == pc1
        assert await fetchval("SELECT property_id FROM property_listings WHERE listing_id=$1", lc2) == pc2
    finally:
        provenance_module.validate_property_merge = original
        await _cleanup(all_lids, all_pids)


# ── 6. stale manifest -> no apply ────────────────────────────────────────

@pytest.mark.asyncio
async def test_stale_manifest_no_apply(db):
    from bot.identity.property_merge_provenance import apply_property_merge_durable
    from bot.db.pg import execute, fetchval, fetchrow

    manifest, la, lb, pa, pb = await _make_pair_and_plan("stale")
    try:
        # Меняем факты ПОСЛЕ снятия manifest'а -> live component_hash разойдётся.
        await execute("UPDATE apartment_listings SET address='ДРУГОЙ адрес, 999' WHERE id=$1", la)

        result = await apply_property_merge_durable(manifest, actor="pytest", dry_run=False,
                                                      git_provenance=_CLEAN_PROVENANCE)
        assert result["status"] == "blocked_stale"

        # manifest ВСЁ РАВНО персистится (задача: "persist ДО apply,
        # независимо от исхода") — только apply отказан.
        m_row = await fetchrow(
            "SELECT manifest_id FROM property_merge_manifest_log WHERE manifest_id=$1", result["manifest_id"])
        assert m_row is not None

        # Никакого repoint не произошло.
        assert await fetchval("SELECT property_id FROM property_listings WHERE listing_id=$1", la) == pa
        assert await fetchval("SELECT property_id FROM property_listings WHERE listing_id=$1", lb) == pb
        assert await fetchval("SELECT identity_status FROM properties WHERE property_id=$1", pa) == "provisional"
        assert await fetchval("SELECT identity_status FROM properties WHERE property_id=$1", pb) == "provisional"
    finally:
        await _cleanup([la, lb], [pa, pb])


# ── 7. dirty git guard — unit-тестируем через injected provider/mock ────────

def test_check_git_provenance_dirty_blocks_without_override():
    from bot.identity.property_merge_provenance import check_git_provenance

    assert check_git_provenance({"git_branch": "master", "git_dirty": False}) == []

    violations = check_git_provenance({"git_branch": "master", "git_dirty": True})
    assert any("dirty" in v for v in violations)

    # Неизвестное состояние (git недоступен) -> fail-closed, тоже блокирует.
    violations_unknown = check_git_provenance({"git_branch": "master", "git_dirty": None})
    assert violations_unknown  # непусто


@pytest.mark.asyncio
async def test_apply_durable_blocked_by_dirty_working_tree(db):
    from bot.identity.property_merge_provenance import apply_property_merge_durable
    from bot.db.pg import fetchval, fetchrow

    manifest, la, lb, pa, pb = await _make_pair_and_plan("dirty")
    try:
        dirty_provenance = {"git_sha": "abc123", "git_branch": "master", "git_dirty": True}
        result = await apply_property_merge_durable(manifest, actor="pytest", dry_run=False,
                                                      git_provenance=dirty_provenance)
        assert result["status"] == "blocked_provenance"
        assert await fetchval("SELECT property_id FROM property_listings WHERE listing_id=$1", la) == pa

        e_row = await fetchrow(
            "SELECT status, error, git_dirty FROM property_merge_execution_log WHERE execution_id=$1",
            result["execution_id"])
        assert e_row["status"] == "blocked_provenance"
        assert "dirty" in e_row["error"]
        assert e_row["git_dirty"] is True
    finally:
        await _cleanup([la, lb], [pa, pb])


# ── 8. non-master requires explicit override ─────────────────────────────

@pytest.mark.asyncio
async def test_non_master_requires_explicit_override(db):
    from bot.identity.property_merge_provenance import apply_property_merge_durable
    from bot.db.pg import fetchval, fetchrow

    manifest, la, lb, pa, pb = await _make_pair_and_plan("nonmaster")
    branch_provenance = {"git_sha": "abc123", "git_branch": "feature/some-branch", "git_dirty": False}
    try:
        blocked = await apply_property_merge_durable(manifest, actor="pytest", dry_run=False,
                                                       git_provenance=branch_provenance)
        assert blocked["status"] == "blocked_provenance"
        assert await fetchval("SELECT property_id FROM property_listings WHERE listing_id=$1", la) == pa

        allowed = await apply_property_merge_durable(
            manifest, actor="pytest", dry_run=False, git_provenance=branch_provenance,
            allow_non_master=True, override_reason="explicit test override — see task 2026-08-30",
        )
        assert allowed["status"] == "merged"
        e_row = await fetchrow(
            "SELECT provenance_override, provenance_override_reason, git_branch "
            "FROM property_merge_execution_log WHERE execution_id=$1", allowed["execution_id"])
        assert e_row["provenance_override"] is True
        assert "override" in e_row["provenance_override_reason"]
        assert e_row["git_branch"] == "feature/some-branch"
    finally:
        await _cleanup([la, lb], [pa, pb])


# ── 9. rollback audit remains append-only ─────────────────────────────────

@pytest.mark.asyncio
async def test_rollback_leaves_manifest_and_execution_log_append_only(db):
    from bot.identity.property_merge import rollback_property_merge
    from bot.identity.property_merge_provenance import apply_property_merge_durable
    from bot.db.pg import fetchrow

    manifest, la, lb, pa, pb = await _make_pair_and_plan("rollback")
    try:
        result = await apply_property_merge_durable(manifest, actor="pytest", dry_run=False,
                                                      git_provenance=_CLEAN_PROVENANCE)
        assert result["status"] == "merged"
        manifest_id, execution_id, group_key = result["manifest_id"], result["execution_id"], result["merge_group_key"]

        before_m = dict(await fetchrow("SELECT * FROM property_merge_manifest_log WHERE manifest_id=$1", manifest_id))
        before_e = dict(await fetchrow("SELECT * FROM property_merge_execution_log WHERE execution_id=$1", execution_id))

        rb = await rollback_property_merge(group_key, actor="pytest", reason="provenance test rollback")
        assert rb["status"] == "rolled_back"

        after_m = dict(await fetchrow("SELECT * FROM property_merge_manifest_log WHERE manifest_id=$1", manifest_id))
        after_e = dict(await fetchrow("SELECT * FROM property_merge_execution_log WHERE execution_id=$1", execution_id))

        assert before_m == after_m, "manifest_log row must never change (append-only)"
        assert before_e == after_e, "execution_log row reflects apply-time truth, rollback is a separate fact"

        pml_row = await fetchrow(
            "SELECT rolled_back_at FROM property_merge_log WHERE merge_group_key=$1", group_key)
        assert pml_row["rolled_back_at"] is not None
    finally:
        await _cleanup([la, lb], [pa, pb])


# ── 10. repeated apply/validation idempotence ─────────────────────────────

@pytest.mark.asyncio
async def test_repeated_apply_and_validation_are_idempotent(db):
    from bot.identity.property_merge_provenance import apply_property_merge_durable, validate_property_merge
    from bot.db.pg import fetchval

    manifest, la, lb, pa, pb = await _make_pair_and_plan("idempotent")
    try:
        first = await apply_property_merge_durable(manifest, actor="pytest", dry_run=False,
                                                     git_provenance=_CLEAN_PROVENANCE)
        assert first["status"] == "merged"
        second = await apply_property_merge_durable(manifest, actor="pytest", dry_run=False,
                                                      git_provenance=_CLEAN_PROVENANCE)
        assert second["status"] == "already_merged"
        assert first["execution_id"] != second["execution_id"]
        assert first["manifest_id"] != second["manifest_id"]  # persist_manifest вызывается КАЖДЫЙ раз

        canonical, losing = first["canonical_property_id"], first["losing_property_ids"][0]
        n_merge_rows = await fetchval(
            "SELECT count(*) FROM property_merge_log WHERE canonical_property_id=$1 AND losing_property_id=$2",
            canonical, losing)
        assert n_merge_rows == 1, "repeated apply must not create a second physical repoint"

        v1 = await validate_property_merge(first["execution_id"])
        v2 = await validate_property_merge(second["execution_id"])
        assert v1["passed"] is True
        assert v2["passed"] is True
        n_validation_rows = await fetchval(
            "SELECT count(*) FROM property_merge_validation_log WHERE execution_id = ANY($1::int[])",
            [first["execution_id"], second["execution_id"]])
        assert n_validation_rows == 2
    finally:
        await _cleanup([la, lb], [pa, pb])


# ── 11+12. fix 2026-08-30 — one resolved git provenance snapshot per call ──
# Regression-тест на конкретный баг, найденный на real canary #1: РАНЬШЕ
# apply_property_merge_durable() вызывал persist_manifest() с СЫРЫМ
# git_provenance (None в обычном production CLI-вызове) ДО того, как сам
# резолвил его через get_git_provenance() парой строк ниже — manifest_log
# получал NULL git_sha/git_branch/git_dirty, а execution_log — реальные
# значения из ОТДЕЛЬНОГО, более позднего вызова get_git_provenance() (два
# независимых снимка одной apply-операции, теоретически способных
# разойтись, если working tree стал dirty МЕЖДУ ними). Существующие тесты
# №1-10 выше этот баг НЕ ловили — все они передают ЯВНЫЙ (не None)
# git_provenance, а баг проявлялся только на None-пути (реальный CLI-
# дефолт) — отсюда специально тест именно на git_provenance=None.

def test_get_git_provenance_returns_well_formed_dict():
    """Чистая проверка реального (не мок) get_git_provenance() — этот файл
    сам живёт в git-репозитории, sha/branch должны резолвиться."""
    from bot.identity.property_merge_provenance import get_git_provenance

    gp = get_git_provenance()
    assert set(gp.keys()) == {"git_sha", "git_branch", "git_dirty"}
    assert gp["git_sha"] is not None
    assert gp["git_branch"] is not None
    assert gp["git_dirty"] in (True, False)


@pytest.mark.asyncio
async def test_manifest_and_execution_share_one_resolved_git_provenance_when_none_passed(db):
    """git_provenance=None -> резолвится РОВНО ОДИН РАЗ реальным
    get_git_provenance() -> manifest_log И execution_log обязаны получить
    ИДЕНТИЧНЫЕ (не просто 'оба не NULL', а побайтово равные) значения —
    единый snapshot на весь durable-вызов. dry_run=True (не требует
    master/clean guard'а — фокус теста на persist-consistency, не на
    guard'е, тот покрыт test_check_git_provenance_* выше)."""
    from bot.identity.property_merge_provenance import apply_property_merge_durable
    from bot.db.pg import fetchrow

    manifest, la, lb, pa, pb = await _make_pair_and_plan("gitnone")
    try:
        result = await apply_property_merge_durable(manifest, actor="pytest", dry_run=True)
        assert result["status"] == "would_apply"

        m_row = await fetchrow(
            "SELECT git_sha, git_branch, git_dirty FROM property_merge_manifest_log WHERE manifest_id=$1",
            result["manifest_id"])
        e_row = await fetchrow(
            "SELECT git_sha, git_branch, git_dirty FROM property_merge_execution_log WHERE execution_id=$1",
            result["execution_id"])

        # Регрессия исходного бага выглядела бы как m_row поля = NULL,
        # пока e_row поля заполнены реальными значениями.
        assert m_row["git_sha"] is not None, "manifest_log.git_sha must be resolved, not NULL (bug regression)"
        assert m_row["git_branch"] is not None, "manifest_log.git_branch must be resolved, not NULL (bug regression)"
        assert m_row["git_dirty"] is not None, "manifest_log.git_dirty must be resolved, not NULL (bug regression)"

        assert m_row["git_sha"] == e_row["git_sha"]
        assert m_row["git_branch"] == e_row["git_branch"]
        assert m_row["git_dirty"] == e_row["git_dirty"]
    finally:
        await _cleanup([la, lb], [pa, pb])


@pytest.mark.asyncio
async def test_explicit_git_provenance_used_consistently_everywhere(db):
    """Явно переданный git_provenance dict (тесты/инжектированный provider)
    -> используется КАК ЕСТЬ, идентично, и в manifest_log, и в
    execution_log — ни один реальный git-subprocess не запускается."""
    from bot.identity.property_merge_provenance import apply_property_merge_durable
    from bot.db.pg import fetchrow

    manifest, la, lb, pa, pb = await _make_pair_and_plan("gitexplicit")
    explicit = {"git_sha": "cafef00d" * 5, "git_branch": "master", "git_dirty": False}
    try:
        result = await apply_property_merge_durable(manifest, actor="pytest", dry_run=False,
                                                      git_provenance=explicit)
        assert result["status"] == "merged"

        m_row = await fetchrow(
            "SELECT git_sha, git_branch, git_dirty FROM property_merge_manifest_log WHERE manifest_id=$1",
            result["manifest_id"])
        e_row = await fetchrow(
            "SELECT git_sha, git_branch, git_dirty FROM property_merge_execution_log WHERE execution_id=$1",
            result["execution_id"])

        for row, label in ((m_row, "manifest_log"), (e_row, "execution_log")):
            assert row["git_sha"] == explicit["git_sha"], label
            assert row["git_branch"] == explicit["git_branch"], label
            assert row["git_dirty"] == explicit["git_dirty"], label
    finally:
        await _cleanup([la, lb], [pa, pb])
