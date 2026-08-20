"""tests/test_property_merge.py — Safe Physical Property Merge (задача
2026-08-20). Synthetic fixtures only (тот же паттерн, что tests/test_
property_match_review.py / tests/test_property_timeline.py) — реальная
Postgres test DB (DATABASE_URL), никакой зависимости от прод-данных: все
id с префиксом '__test_pmerge_...__', удаляются в finally."""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

_BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _dt(days: float) -> datetime:
    return _BASE + timedelta(days=days)


@pytest_asyncio.fixture
async def db():
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    yield
    await close_pool()


async def _insert_listing(lid, *, address="Тест, 1", floor=5, area=45.0, rooms=2, price=20000000,
                           seller_name=None, is_active=True, first_seen=None, last_seen=None,
                           archived_at=None, archive_reason=None, lat=None, lon=None):
    from bot.db.pg import execute
    await execute(
        """
        INSERT INTO apartment_listings (id, url, address, floor, area, rooms, price, seller_name,
                                         is_active, first_seen, last_seen, archived_at, archive_reason, lat, lon)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
        ON CONFLICT (id) DO UPDATE SET address=$3, floor=$4, area=$5, rooms=$6, price=$7,
            seller_name=$8, is_active=$9, first_seen=$10, last_seen=$11, archived_at=$12, archive_reason=$13,
            lat=$14, lon=$15
        """,
        lid, f"https://krisha.kz/test/{lid}", address, floor, area, rooms, price, seller_name,
        is_active, first_seen, last_seen, archived_at, archive_reason, lat, lon,
    )


async def _make_property(address_hash, *, floor=5, area_sqm=45.0, rooms=2, complex_id=None,
                          identity_status="provisional", first_seen_at=None, last_seen_at=None):
    from bot.db.pg import fetchval
    return await fetchval(
        "INSERT INTO properties (address_hash, floor, area_sqm, rooms, complex_id, identity_status, "
        "first_seen_at, last_seen_at) "
        "VALUES ($1,$2,$3,$4,$5,$6, COALESCE($7, now()), COALESCE($8, now())) RETURNING property_id",
        address_hash, floor, area_sqm, rooms, complex_id, identity_status, first_seen_at, last_seen_at,
    )


async def _link(property_id, listing_id, *, link_method="bootstrap", confidence=1.0):
    from bot.db.pg import execute
    await execute(
        "INSERT INTO property_listings (property_id, listing_id, link_method, confidence) "
        "VALUES ($1,$2,$3,$4)",
        property_id, listing_id, link_method, confidence,
    )


async def _make_candidate(listing_id, candidate_property_id, *, match_method="exact_hash", match_score=0.9,
                           relationship_type="unknown", status="pending", matcher_version="test_v1",
                           conflict_reasons=None):
    from bot.db.pg import fetchval
    import json as _json
    return await fetchval(
        """
        INSERT INTO property_match_candidates
            (listing_id, candidate_property_id, match_method, match_score, relationship_type,
             evidence, conflict_reasons, matcher_version, status)
        VALUES ($1,$2,$3,$4,$5,'{}'::jsonb,$6::jsonb,$7,$8)
        RETURNING candidate_id
        """,
        listing_id, candidate_property_id, match_method, match_score, relationship_type,
        _json.dumps(conflict_reasons) if conflict_reasons else None, matcher_version, status,
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
            (candidate_id, listing_id, candidate_property_id, decision, matcher_version,
             evidence_snapshot, reviewed_by)
        VALUES ($1,$2,$3,'accepted',$4,$5,$6)
        """,
        c["candidate_id"], c["listing_id"], c["candidate_property_id"], c["matcher_version"],
        c["evidence"], reviewed_by,
    )


async def _cleanup(listing_ids, property_ids):
    from bot.db.pg import execute
    lids, pids = list(listing_ids), list(property_ids)
    await execute(
        "DELETE FROM property_merge_log WHERE canonical_property_id = ANY($1::int[]) "
        "OR losing_property_id = ANY($1::int[])", pids)
    await execute(
        "DELETE FROM property_match_review_log WHERE candidate_property_id = ANY($1::int[]) "
        "OR listing_id = ANY($2::text[])", pids, lids)
    await execute(
        "DELETE FROM property_candidate_photo_evidence WHERE candidate_id IN "
        "(SELECT candidate_id FROM property_match_candidates "
        " WHERE listing_id = ANY($1::text[]) OR candidate_property_id = ANY($2::int[]))", lids, pids)
    await execute(
        "DELETE FROM property_match_candidates WHERE listing_id = ANY($1::text[]) "
        "OR candidate_property_id = ANY($2::int[])", lids, pids)
    await execute("DELETE FROM price_history WHERE listing_id = ANY($1::text[])", lids)
    await execute("DELETE FROM listing_archive_history WHERE listing_id = ANY($1::text[])", lids)
    await execute("DELETE FROM property_listings WHERE listing_id = ANY($1::text[])", lids)
    await execute("DELETE FROM properties WHERE property_id = ANY($1::int[])", pids)
    await execute("DELETE FROM apartment_listings WHERE id = ANY($1::text[])", lids)


def _find_component_plan(plans, property_id):
    for p in plans:
        if property_id in p["members"]:
            return p
    return None


# ── 1. merge двух provisional properties ────────────────────────────────

@pytest.mark.asyncio
async def test_merge_two_provisional_properties(db):
    from bot.identity.property_merge import plan_property_merge, apply_property_merge, save_manifest, load_manifest

    la, lb = "__test_pmerge_2p_a__", "__test_pmerge_2p_b__"
    pa = pb = None
    try:
        await _insert_listing(la, address="Абая, 1", first_seen=_dt(0), last_seen=_dt(5))
        await _insert_listing(lb, address="Абая, 1", first_seen=_dt(10), last_seen=_dt(15))
        pa = await _make_property("__test_pmerge_2p_hash_a__", first_seen_at=_dt(0), last_seen_at=_dt(5))
        pb = await _make_property("__test_pmerge_2p_hash_b__", first_seen_at=_dt(10), last_seen_at=_dt(15))
        await _link(pa, la)
        await _link(pb, lb)
        cid = await _make_candidate(la, pb, relationship_type="relist", status="pending")
        await _accept(cid)

        plans = await plan_property_merge({pa, pb})
        plan = _find_component_plan(plans, pa)
        assert plan["status"] == "planned"
        assert set(plan["members"]) == {pa, pb}

        manifest = plan["manifest"]
        result = await apply_property_merge(manifest, actor="pytest", dry_run=False)
        assert result["status"] == "merged"
        canonical = result["canonical_property_id"]
        losing = result["losing_property_ids"][0]
        assert {canonical, losing} == {pa, pb}

        from bot.db.pg import fetchval
        assert await fetchval("SELECT property_id FROM property_listings WHERE listing_id=$1", la) == canonical
        assert await fetchval("SELECT property_id FROM property_listings WHERE listing_id=$1", lb) == canonical
        assert await fetchval("SELECT identity_status FROM properties WHERE property_id=$1", losing) == "merged"
        assert await fetchval("SELECT identity_status FROM properties WHERE property_id=$1", canonical) == "provisional"
    finally:
        await _cleanup([la, lb], [p for p in (pa, pb) if p])


# ── 2. chain A-B-C ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_chain_a_b_c_forms_one_component(db):
    from bot.identity.property_merge import plan_property_merge, apply_property_merge

    la, lb, lc = "__test_pmerge_chain_a__", "__test_pmerge_chain_b__", "__test_pmerge_chain_c__"
    pa = pb = pc = None
    try:
        await _insert_listing(la, address="Чейн, 1", first_seen=_dt(0), last_seen=_dt(5))
        await _insert_listing(lb, address="Чейн, 1", first_seen=_dt(10), last_seen=_dt(15))
        await _insert_listing(lc, address="Чейн, 1", first_seen=_dt(20), last_seen=_dt(25))
        pa = await _make_property("__test_pmerge_chain_hash_a__", first_seen_at=_dt(0), last_seen_at=_dt(5))
        pb = await _make_property("__test_pmerge_chain_hash_b__", first_seen_at=_dt(10), last_seen_at=_dt(15))
        pc = await _make_property("__test_pmerge_chain_hash_c__", first_seen_at=_dt(20), last_seen_at=_dt(25))
        await _link(pa, la); await _link(pb, lb); await _link(pc, lc)

        # A-B ребро и B-C ребро — НЕТ прямого A-C, но union-find должен
        # объединить транзитивно в ОДИН компонент.
        c1 = await _make_candidate(la, pb, relationship_type="relist")
        await _accept(c1)
        c2 = await _make_candidate(lb, pc, relationship_type="relist")
        await _accept(c2)

        plans = await plan_property_merge({pa, pb, pc})
        plan = _find_component_plan(plans, pa)
        assert plan["status"] == "planned"
        assert set(plan["members"]) == {pa, pb, pc}
        assert len(plan["losing_property_ids"]) == 2

        result = await apply_property_merge(plan["manifest"], actor="pytest", dry_run=False)
        assert result["status"] == "merged"
        assert len(result["losing_property_ids"]) == 2

        from bot.db.pg import fetchval
        canonical = result["canonical_property_id"]
        for lid in (la, lb, lc):
            assert await fetchval("SELECT property_id FROM property_listings WHERE listing_id=$1", lid) == canonical
    finally:
        await _cleanup([la, lb, lc], [p for p in (pa, pb, pc) if p])


# ── 3. компонент с 3+ accepted рёбрами (треугольник, редундантные рёбра) ─

@pytest.mark.asyncio
async def test_component_with_redundant_triangle_edges(db):
    from bot.identity.property_merge import plan_property_merge, apply_property_merge

    la, lb, lc = "__test_pmerge_tri_a__", "__test_pmerge_tri_b__", "__test_pmerge_tri_c__"
    pa = pb = pc = None
    try:
        await _insert_listing(la, address="Треугол, 1", first_seen=_dt(0), last_seen=_dt(5))
        await _insert_listing(lb, address="Треугол, 1", first_seen=_dt(10), last_seen=_dt(15))
        await _insert_listing(lc, address="Треугол, 1", first_seen=_dt(20), last_seen=_dt(25))
        pa = await _make_property("__test_pmerge_tri_hash_a__", first_seen_at=_dt(0), last_seen_at=_dt(5))
        pb = await _make_property("__test_pmerge_tri_hash_b__", first_seen_at=_dt(10), last_seen_at=_dt(15))
        pc = await _make_property("__test_pmerge_tri_hash_c__", first_seen_at=_dt(20), last_seen_at=_dt(25))
        await _link(pa, la); await _link(pb, lb); await _link(pc, lc)

        # ВСЕ ТРИ ребра — A-B, B-C, A-C (треугольник, edge_count > node_count-1).
        c1 = await _make_candidate(la, pb, relationship_type="relist")
        await _accept(c1)
        c2 = await _make_candidate(lb, pc, relationship_type="relist")
        await _accept(c2)
        c3 = await _make_candidate(lc, pa, relationship_type="relist")
        await _accept(c3)

        plans = await plan_property_merge({pa, pb, pc})
        plan = _find_component_plan(plans, pa)
        assert plan["status"] == "planned"
        assert set(plan["members"]) == {pa, pb, pc}  # ОДИН компонент, не три отдельных
        assert set(plan["manifest"]["candidate_ids"]) == {c1, c2, c3}

        result = await apply_property_merge(plan["manifest"], actor="pytest", dry_run=False)
        assert result["status"] == "merged"
        assert len(result["losing_property_ids"]) == 2  # 3 properties -> 1 canonical + 2 losing
    finally:
        await _cleanup([la, lb, lc], [p for p in (pa, pb, pc) if p])


# ── 4. deterministic canonical selection ────────────────────────────────

@pytest.mark.asyncio
async def test_canonical_selection_is_deterministic_and_repeatable(db):
    from bot.identity.property_merge import score_canonical_candidates, _load_component_facts

    la, lb = "__test_pmerge_det_a__", "__test_pmerge_det_b__"
    pa = pb = None
    try:
        # A: более длинная история + координаты. B: короче, без координат.
        await _insert_listing(la, address="Детерм, 1", first_seen=_dt(0), last_seen=_dt(100), lat=51.1, lon=71.4)
        await _insert_listing(lb, address="Детерм, 1", first_seen=_dt(50), last_seen=_dt(60))
        pa = await _make_property("__test_pmerge_det_hash_a__", first_seen_at=_dt(0), last_seen_at=_dt(100))
        pb = await _make_property("__test_pmerge_det_hash_b__", first_seen_at=_dt(50), last_seen_at=_dt(60))
        await _link(pa, la); await _link(pb, lb)

        facts = await _load_component_facts({pa, pb})
        scored1 = score_canonical_candidates({pa, pb}, facts)
        scored2 = score_canonical_candidates({pa, pb}, facts)
        assert scored1 == scored2  # чистая функция, детерминированная
        assert scored1[0]["property_id"] == pa  # дольше история + координаты -> выше score
    finally:
        await _cleanup([la, lb], [p for p in (pa, pb) if p])


@pytest.mark.asyncio
async def test_canonical_tie_break_is_smaller_property_id(db):
    from bot.identity.property_merge import score_canonical_candidates, _load_component_facts

    la, lb = "__test_pmerge_tie_a__", "__test_pmerge_tie_b__"
    pa = pb = None
    try:
        # Полностью идентичные факторы -> tie-break по property_id.
        await _insert_listing(la, address="Тай, 1", first_seen=_dt(0), last_seen=_dt(10))
        await _insert_listing(lb, address="Тай, 1", first_seen=_dt(0), last_seen=_dt(10))
        pa = await _make_property("__test_pmerge_tie_hash_a__", first_seen_at=_dt(0), last_seen_at=_dt(10))
        pb = await _make_property("__test_pmerge_tie_hash_b__", first_seen_at=_dt(0), last_seen_at=_dt(10))
        await _link(pa, la); await _link(pb, lb)

        facts = await _load_component_facts({pa, pb})
        scored = score_canonical_candidates({pa, pb}, facts)
        assert scored[0]["property_id"] == min(pa, pb)
    finally:
        await _cleanup([la, lb], [p for p in (pa, pb) if p])


@pytest.mark.asyncio
async def test_confirmed_identity_status_preferred_over_provisional(db):
    """Расхождение 1 (см. bot/identity/property_merge.py докстринг) —
    identity_status тир ПЕРЕД 7-факторным score."""
    from bot.identity.property_merge import score_canonical_candidates, _load_component_facts

    la, lb = "__test_pmerge_conf_a__", "__test_pmerge_conf_b__"
    pa = pb = None
    try:
        # B выигрывал бы по 7-факторному score (длиннее история), но A confirmed.
        await _insert_listing(la, address="Конф, 1", first_seen=_dt(0), last_seen=_dt(5))
        await _insert_listing(lb, address="Конф, 1", first_seen=_dt(0), last_seen=_dt(200))
        pa = await _make_property("__test_pmerge_conf_hash_a__", identity_status="confirmed",
                                   first_seen_at=_dt(0), last_seen_at=_dt(5))
        pb = await _make_property("__test_pmerge_conf_hash_b__", identity_status="provisional",
                                   first_seen_at=_dt(0), last_seen_at=_dt(200))
        await _link(pa, la); await _link(pb, lb)

        facts = await _load_component_facts({pa, pb})
        scored = score_canonical_candidates({pa, pb}, facts)
        assert scored[0]["property_id"] == pa  # confirmed побеждает несмотря на более низкий 7-факторный score
    finally:
        await _cleanup([la, lb], [p for p in (pa, pb) if p])


# ── 5. pending edge внутри компонента не применяется ────────────────────

@pytest.mark.asyncio
async def test_pending_edge_does_not_join_component(db):
    from bot.identity.property_merge import plan_property_merge

    la, lb, lc = "__test_pmerge_pend_a__", "__test_pmerge_pend_b__", "__test_pmerge_pend_c__"
    pa = pb = pc = None
    try:
        await _insert_listing(la, address="Пенд, 1", first_seen=_dt(0), last_seen=_dt(5))
        await _insert_listing(lb, address="Пенд, 1", first_seen=_dt(10), last_seen=_dt(15))
        await _insert_listing(lc, address="Пенд, 1", first_seen=_dt(20), last_seen=_dt(25))
        pa = await _make_property("__test_pmerge_pend_hash_a__", first_seen_at=_dt(0), last_seen_at=_dt(5))
        pb = await _make_property("__test_pmerge_pend_hash_b__", first_seen_at=_dt(10), last_seen_at=_dt(15))
        pc = await _make_property("__test_pmerge_pend_hash_c__", first_seen_at=_dt(20), last_seen_at=_dt(25))
        await _link(pa, la); await _link(pb, lb); await _link(pc, lc)

        c1 = await _make_candidate(la, pb, relationship_type="relist")
        await _accept(c1)
        # B-C остаётся PENDING — НЕ должен присоединить C к компоненту.
        await _make_candidate(lb, pc, relationship_type="relist", status="pending")

        plans = await plan_property_merge({pa, pb, pc})
        plan = _find_component_plan(plans, pa)
        assert set(plan["members"]) == {pa, pb}  # C не вошёл
        assert _find_component_plan(plans, pc) is None or pc not in _find_component_plan(plans, pc)["members"] \
            if _find_component_plan(plans, pc) else True
    finally:
        await _cleanup([la, lb, lc], [p for p in (pa, pb, pc) if p])


# ── 6. rejected/deferred не применяется ─────────────────────────────────

@pytest.mark.asyncio
async def test_rejected_edge_does_not_join_component(db):
    from bot.identity.property_merge import plan_property_merge

    la, lb = "__test_pmerge_rej_a__", "__test_pmerge_rej_b__"
    pa = pb = None
    try:
        await _insert_listing(la, address="Реж, 1", first_seen=_dt(0), last_seen=_dt(5))
        await _insert_listing(lb, address="Реж, 1", first_seen=_dt(10), last_seen=_dt(15))
        pa = await _make_property("__test_pmerge_rej_hash_a__", first_seen_at=_dt(0), last_seen_at=_dt(5))
        pb = await _make_property("__test_pmerge_rej_hash_b__", first_seen_at=_dt(10), last_seen_at=_dt(15))
        await _link(pa, la); await _link(pb, lb)
        await _make_candidate(la, pb, relationship_type="relist", status="rejected")

        plans = await plan_property_merge({pa, pb})
        assert plans == []  # ни одного accepted ребра -> ни одного компонента
    finally:
        await _cleanup([la, lb], [p for p in (pa, pb) if p])


@pytest.mark.asyncio
async def test_deferred_skip_only_edge_does_not_join_component(db):
    """'Отложено' (skip, задача 2026-08-18) — status остаётся 'pending',
    только запись в review_log — тоже НЕ должно мерджиться."""
    from bot.identity.property_merge import plan_property_merge
    from bot.db.pg import execute

    la, lb = "__test_pmerge_defer_a__", "__test_pmerge_defer_b__"
    pa = pb = None
    try:
        await _insert_listing(la, address="Отлож, 1", first_seen=_dt(0), last_seen=_dt(5))
        await _insert_listing(lb, address="Отлож, 1", first_seen=_dt(10), last_seen=_dt(15))
        pa = await _make_property("__test_pmerge_defer_hash_a__", first_seen_at=_dt(0), last_seen_at=_dt(5))
        pb = await _make_property("__test_pmerge_defer_hash_b__", first_seen_at=_dt(10), last_seen_at=_dt(15))
        await _link(pa, la); await _link(pb, lb)
        cid = await _make_candidate(la, pb, relationship_type="relist", status="pending")
        await execute(
            "INSERT INTO property_match_review_log (candidate_id, listing_id, candidate_property_id, "
            "decision, matcher_version, reviewed_by) VALUES ($1,$2,$3,'skip','test_v1','pytest')",
            cid, la, pb)

        plans = await plan_property_merge({pa, pb})
        assert plans == []  # skip НЕ меняет status на accepted — остаётся pending, не мерджится
    finally:
        await _cleanup([la, lb], [p for p in (pa, pb) if p])


# ── 7. stale relationship_type + current hard conflict → BLOCK ─────────

@pytest.mark.asyncio
async def test_stale_relationship_type_with_current_hard_conflict_blocks(db):
    """Прямой аналог реальной находки на проде — candidate_id=316
    (см. bot/identity/property_merge.py докстринг): stored relationship_
    type='relist' на момент accept, ТЕКУЩИЕ данные показывают rooms
    mismatch (значения изменились ПОСЛЕ решения человека) -> BLOCK, не
    трогая существующее accepted-решение."""
    from bot.identity.property_merge import plan_property_merge

    la, lb = "__test_pmerge_stale_a__", "__test_pmerge_stale_b__"
    pa = pb = None
    try:
        await _insert_listing(la, address="Стейл, 1", rooms=3, first_seen=_dt(0), archived_at=_dt(5), last_seen=_dt(5))
        await _insert_listing(lb, address="Стейл, 1", rooms=3, first_seen=_dt(10), last_seen=_dt(15))
        pa = await _make_property("__test_pmerge_stale_hash_a__", rooms=3, first_seen_at=_dt(0), last_seen_at=_dt(5))
        pb = await _make_property("__test_pmerge_stale_hash_b__", rooms=3, first_seen_at=_dt(10), last_seen_at=_dt(15))
        await _link(pa, la); await _link(pb, lb)
        cid = await _make_candidate(la, pb, relationship_type="relist", status="pending")
        await _accept(cid)  # accepted, когда rooms ЕЩЁ совпадали (3 vs 3)

        # ПОСЛЕ решения — данные листинга A обновились (перескрап/правка продавца).
        await _insert_listing(la, address="Стейл, 1", rooms=2, first_seen=_dt(0), archived_at=_dt(5), last_seen=_dt(5))

        plans = await plan_property_merge({pa, pb})
        plan = _find_component_plan(plans, pa)
        assert plan["status"] == "blocked"
        reasons = {r["reason"] for r in plan["blocked_reasons"]}
        assert "rooms_mismatch" in reasons
    finally:
        await _cleanup([la, lb], [p for p in (pa, pb) if p])


# ── 8. concurrent listings допустимы, не конфликт сами по себе ─────────

@pytest.mark.asyncio
async def test_concurrent_overlapping_listings_are_not_a_conflict(db):
    from bot.identity.property_merge import plan_property_merge

    la, lb = "__test_pmerge_conc_a__", "__test_pmerge_conc_b__"
    pa = pb = None
    try:
        # [0,10] и [5,15] ПЕРЕСЕКАЮТСЯ — одновременно активны, разные агенты.
        await _insert_listing(la, address="Конк, 1", first_seen=_dt(0), archived_at=_dt(10), last_seen=_dt(10))
        await _insert_listing(lb, address="Конк, 1", first_seen=_dt(5), archived_at=_dt(15), last_seen=_dt(15))
        pa = await _make_property("__test_pmerge_conc_hash_a__", first_seen_at=_dt(0), last_seen_at=_dt(10))
        pb = await _make_property("__test_pmerge_conc_hash_b__", first_seen_at=_dt(5), last_seen_at=_dt(15))
        await _link(pa, la); await _link(pb, lb)
        cid = await _make_candidate(la, pb, relationship_type="concurrent_duplicate")
        await _accept(cid)

        plans = await plan_property_merge({pa, pb})
        plan = _find_component_plan(plans, pa)
        assert plan["status"] == "planned"  # пересечение по времени само по себе НЕ блокирует
    finally:
        await _cleanup([la, lb], [p for p in (pa, pb) if p])


# ── 9. rooms mismatch → BLOCK ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_rooms_mismatch_blocks(db):
    from bot.identity.property_merge import plan_property_merge

    la, lb = "__test_pmerge_rooms_a__", "__test_pmerge_rooms_b__"
    pa = pb = None
    try:
        await _insert_listing(la, address="Рум, 1", rooms=2, first_seen=_dt(0), last_seen=_dt(5))
        await _insert_listing(lb, address="Рум, 1", rooms=4, first_seen=_dt(10), last_seen=_dt(15))
        pa = await _make_property("__test_pmerge_rooms_hash_a__", rooms=2, first_seen_at=_dt(0), last_seen_at=_dt(5))
        pb = await _make_property("__test_pmerge_rooms_hash_b__", rooms=4, first_seen_at=_dt(10), last_seen_at=_dt(15))
        await _link(pa, la); await _link(pb, lb)
        cid = await _make_candidate(la, pb, relationship_type="relist")
        await _accept(cid)

        plans = await plan_property_merge({pa, pb})
        plan = _find_component_plan(plans, pa)
        assert plan["status"] == "blocked"
        assert plan["manifest"] is None
        assert any(r["reason"] == "rooms_mismatch" for r in plan["blocked_reasons"])
    finally:
        await _cleanup([la, lb], [p for p in (pa, pb) if p])


# ── 10. severe address/house mismatch → BLOCK ───────────────────────────

@pytest.mark.asyncio
async def test_severe_address_mismatch_without_shared_complex_blocks(db):
    from bot.identity.property_merge import plan_property_merge

    la, lb = "__test_pmerge_addr_a__", "__test_pmerge_addr_b__"
    pa = pb = None
    try:
        await _insert_listing(la, address="Абая 5", first_seen=_dt(0), last_seen=_dt(5))
        await _insert_listing(lb, address="Сатпаева 12", first_seen=_dt(10), last_seen=_dt(15))
        pa = await _make_property("__test_pmerge_addr_hash_a__", complex_id=None,
                                   first_seen_at=_dt(0), last_seen_at=_dt(5))
        pb = await _make_property("__test_pmerge_addr_hash_b__", complex_id=None,
                                   first_seen_at=_dt(10), last_seen_at=_dt(15))
        await _link(pa, la); await _link(pb, lb)
        cid = await _make_candidate(la, pb, relationship_type="relist")
        await _accept(cid)

        plans = await plan_property_merge({pa, pb})
        plan = _find_component_plan(plans, pa)
        assert plan["status"] == "blocked"
        assert any(r["reason"] == "severe_address_mismatch" for r in plan["blocked_reasons"])
    finally:
        await _cleanup([la, lb], [p for p in (pa, pb) if p])


@pytest.mark.asyncio
async def test_house_number_mismatch_with_shared_complex_id_does_not_block(db):
    """Прямая регрессия на реальную находку (63/129 accepted пар на проде
    имеют 'house number mismatch' по сырому extract_house_number — 62/63
    из них — ОДИН ЖК с разной нотацией подъезда/корпуса, НЕ разные дома,
    см. модульный докстринг 'Расхождение 3')."""
    from bot.identity.property_merge import plan_property_merge

    la, lb = "__test_pmerge_samecx_a__", "__test_pmerge_samecx_b__"
    pa = pb = None
    try:
        await _insert_listing(la, address="Сыганак 25К1", first_seen=_dt(0), last_seen=_dt(5))
        await _insert_listing(lb, address="Сыганак 25/1", first_seen=_dt(10), last_seen=_dt(15))
        pa = await _make_property("__test_pmerge_samecx_hash_a__", complex_id=2070,
                                   first_seen_at=_dt(0), last_seen_at=_dt(5))
        pb = await _make_property("__test_pmerge_samecx_hash_b__", complex_id=2070,
                                   first_seen_at=_dt(10), last_seen_at=_dt(15))
        await _link(pa, la); await _link(pb, lb)
        cid = await _make_candidate(la, pb, relationship_type="relist")
        await _accept(cid)

        plans = await plan_property_merge({pa, pb})
        plan = _find_component_plan(plans, pa)
        assert plan["status"] == "planned"  # общий complex_id перевешивает формальный house-number mismatch
    finally:
        await _cleanup([la, lb], [p for p in (pa, pb) if p])


# ── 11. transaction rollback при ошибке на середине ─────────────────────

@pytest.mark.asyncio
async def test_execute_merge_is_atomic_on_mid_loop_error(db, monkeypatch):
    """3 properties (1 canonical + 2 losing) — форсированная ошибка
    ПОСЛЕ того, как первая losing-property уже была repointed внутри
    транзакции, но ДО commit — весь merge должен откатиться целиком, НЕ
    оставить одну losing property смерженной, а другую нет."""
    import bot.identity.property_merge as property_merge

    la, lb, lc = "__test_pmerge_atomic_a__", "__test_pmerge_atomic_b__", "__test_pmerge_atomic_c__"
    pa = pb = pc = None
    try:
        await _insert_listing(la, address="Атом, 1", first_seen=_dt(0), last_seen=_dt(100))  # длиннее история -> canonical
        await _insert_listing(lb, address="Атом, 1", first_seen=_dt(10), last_seen=_dt(15))
        await _insert_listing(lc, address="Атом, 1", first_seen=_dt(20), last_seen=_dt(25))
        pa = await _make_property("__test_pmerge_atomic_hash_a__", first_seen_at=_dt(0), last_seen_at=_dt(100))
        pb = await _make_property("__test_pmerge_atomic_hash_b__", first_seen_at=_dt(10), last_seen_at=_dt(15))
        pc = await _make_property("__test_pmerge_atomic_hash_c__", first_seen_at=_dt(20), last_seen_at=_dt(25))
        await _link(pa, la); await _link(pb, lb); await _link(pc, lc)
        c1 = await _make_candidate(la, pb, relationship_type="relist")
        await _accept(c1)
        c2 = await _make_candidate(la, pc, relationship_type="relist")
        await _accept(c2)

        from bot.identity.property_merge import plan_property_merge
        plans = await plan_property_merge({pa, pb, pc})
        plan = _find_component_plan(plans, pa)
        assert plan["status"] == "planned"
        assert plan["canonical_property_id"] == pa

        call_count = {"n": 0}
        real_dumps = property_merge.json.dumps

        def _flaky_dumps(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 3:  # 2 вызова на первую losing property (moved+decision_source) + 1-й на вторую
                raise RuntimeError("injected mid-transaction failure")
            return real_dumps(*args, **kwargs)

        monkeypatch.setattr(property_merge.json, "dumps", _flaky_dumps)

        with pytest.raises(RuntimeError, match="injected mid-transaction failure"):
            await property_merge.apply_property_merge(plan["manifest"], actor="pytest", dry_run=False)

        monkeypatch.undo()

        from bot.db.pg import fetchval
        # НИ ОДНА losing property НЕ должна была реально репойнтнуться —
        # атомарность всей транзакции, не только "первая половина откатилась".
        assert await fetchval("SELECT property_id FROM property_listings WHERE listing_id=$1", lb) == pb
        assert await fetchval("SELECT property_id FROM property_listings WHERE listing_id=$1", lc) == pc
        assert await fetchval("SELECT identity_status FROM properties WHERE property_id=$1", pb) == "provisional"
        assert await fetchval("SELECT identity_status FROM properties WHERE property_id=$1", pc) == "provisional"
        assert await fetchval(
            "SELECT count(*) FROM property_merge_log WHERE canonical_property_id=$1", pa) == 0
    finally:
        await _cleanup([la, lb, lc], [p for p in (pa, pb, pc) if p])


# ── 12. repeated apply ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_repeated_apply_is_idempotent_no_duplicate_log_rows(db):
    from bot.identity.property_merge import plan_property_merge, apply_property_merge

    la, lb = "__test_pmerge_idem_a__", "__test_pmerge_idem_b__"
    pa = pb = None
    try:
        await _insert_listing(la, address="Идем, 1", first_seen=_dt(0), last_seen=_dt(5))
        await _insert_listing(lb, address="Идем, 1", first_seen=_dt(10), last_seen=_dt(15))
        pa = await _make_property("__test_pmerge_idem_hash_a__", first_seen_at=_dt(0), last_seen_at=_dt(5))
        pb = await _make_property("__test_pmerge_idem_hash_b__", first_seen_at=_dt(10), last_seen_at=_dt(15))
        await _link(pa, la); await _link(pb, lb)
        cid = await _make_candidate(la, pb, relationship_type="relist")
        await _accept(cid)

        plans = await plan_property_merge({pa, pb})
        manifest = _find_component_plan(plans, pa)["manifest"]

        r1 = await apply_property_merge(manifest, actor="pytest", dry_run=False)
        assert r1["status"] == "merged"

        r2 = await apply_property_merge(manifest, actor="pytest", dry_run=False)
        assert r2["status"] == "already_merged"

        from bot.db.pg import fetchval
        n = await fetchval(
            "SELECT count(*) FROM property_merge_log WHERE merge_group_key = $1", r1["merge_group_key"])
        assert n == 1  # ОДНА losing property -> одна строка, не задвоилась
    finally:
        await _cleanup([la, lb], [p for p in (pa, pb) if p])


@pytest.mark.asyncio
async def test_apply_blocks_when_component_facts_drifted_since_plan(db):
    """Frozen manifest workflow — если факты изменились ПОСЛЕ plan (но
    ДО apply), apply должен fail-closed, не молча смерджить по устаревшему
    манифесту (задача: 'не повторять ошибку photo-canary с
    live-reselecting query')."""
    from bot.identity.property_merge import plan_property_merge, apply_property_merge

    la, lb = "__test_pmerge_drift_a__", "__test_pmerge_drift_b__"
    pa = pb = None
    try:
        await _insert_listing(la, address="Дрифт, 1", rooms=2, first_seen=_dt(0), last_seen=_dt(5))
        await _insert_listing(lb, address="Дрифт, 1", rooms=2, first_seen=_dt(10), last_seen=_dt(15))
        pa = await _make_property("__test_pmerge_drift_hash_a__", rooms=2, first_seen_at=_dt(0), last_seen_at=_dt(5))
        pb = await _make_property("__test_pmerge_drift_hash_b__", rooms=2, first_seen_at=_dt(10), last_seen_at=_dt(15))
        await _link(pa, la); await _link(pb, lb)
        cid = await _make_candidate(la, pb, relationship_type="relist")
        await _accept(cid)

        plans = await plan_property_merge({pa, pb})
        manifest = _find_component_plan(plans, pa)["manifest"]

        # Данные "утекли" ПОСЛЕ plan — цена листинга A резко изменилась.
        await _insert_listing(la, address="Дрифт, 1", rooms=2, price=99999999,
                               first_seen=_dt(0), last_seen=_dt(5))

        result = await apply_property_merge(manifest, actor="pytest", dry_run=False)
        assert result["status"] == "blocked_stale"

        from bot.db.pg import fetchval
        assert await fetchval("SELECT property_id FROM property_listings WHERE listing_id=$1", lb) == pb
    finally:
        await _cleanup([la, lb], [p for p in (pa, pb) if p])


# ── 13. rollback operation ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rollback_restores_previous_mapping(db):
    from bot.identity.property_merge import plan_property_merge, apply_property_merge, rollback_property_merge

    la, lb = "__test_pmerge_rb_a__", "__test_pmerge_rb_b__"
    pa = pb = None
    try:
        await _insert_listing(la, address="Ролбэк, 1", first_seen=_dt(0), last_seen=_dt(5))
        await _insert_listing(lb, address="Ролбэк, 1", first_seen=_dt(10), last_seen=_dt(15))
        pa = await _make_property("__test_pmerge_rb_hash_a__", first_seen_at=_dt(0), last_seen_at=_dt(5))
        pb = await _make_property("__test_pmerge_rb_hash_b__", first_seen_at=_dt(10), last_seen_at=_dt(15))
        await _link(pa, la); await _link(pb, lb)
        cid = await _make_candidate(la, pb, relationship_type="relist")
        await _accept(cid)

        plans = await plan_property_merge({pa, pb})
        manifest = _find_component_plan(plans, pa)["manifest"]
        merged = await apply_property_merge(manifest, actor="pytest", dry_run=False)
        assert merged["status"] == "merged"
        canonical, losing = merged["canonical_property_id"], merged["losing_property_ids"][0]

        rb = await rollback_property_merge(merged["merge_group_key"], actor="pytest", reason="test rollback")
        assert rb["status"] == "rolled_back"

        from bot.db.pg import fetchval
        original_owner_a = pa if losing == pa else pb
        original_owner_b = pa if losing == pb else pb
        # Оба listing вернулись на исходные (pre-merge) property_id.
        assert await fetchval("SELECT property_id FROM property_listings WHERE listing_id=$1", la) in (pa, pb)
        assert await fetchval("SELECT property_id FROM property_listings WHERE listing_id=$1", lb) in (pa, pb)
        assert await fetchval("SELECT identity_status FROM properties WHERE property_id=$1", losing) == "provisional"

        rolled_back_at = await fetchval(
            "SELECT rolled_back_at FROM property_merge_log WHERE merge_group_key=$1", merged["merge_group_key"])
        assert rolled_back_at is not None

        # Повторный rollback — идемпотентный no-op, не ошибка.
        rb2 = await rollback_property_merge(merged["merge_group_key"], actor="pytest", reason="again")
        assert rb2["status"] == "not_found_or_already_rolled_back"
    finally:
        await _cleanup([la, lb], [p for p in (pa, pb) if p])


# ── 14/15. Property Timeline после merge ────────────────────────────────

@pytest.mark.asyncio
async def test_timeline_after_merge_sees_all_original_listings(db):
    from bot.identity.property_merge import plan_property_merge, apply_property_merge
    from bot.core.property_timeline import build_property_timeline

    la, lb = "__test_pmerge_tl_a__", "__test_pmerge_tl_b__"
    pa = pb = None
    try:
        await _insert_listing(la, address="Таймлайн, 1", first_seen=_dt(0), archived_at=_dt(10), last_seen=_dt(10))
        await _insert_listing(lb, address="Таймлайн, 1", first_seen=_dt(20), last_seen=_dt(30))
        pa = await _make_property("__test_pmerge_tl_hash_a__", first_seen_at=_dt(0), last_seen_at=_dt(10))
        pb = await _make_property("__test_pmerge_tl_hash_b__", first_seen_at=_dt(20), last_seen_at=_dt(30))
        await _link(pa, la); await _link(pb, lb)
        cid = await _make_candidate(la, pb, relationship_type="relist")
        await _accept(cid)

        plans = await plan_property_merge({pa, pb})
        manifest = _find_component_plan(plans, pa)["manifest"]
        merged = await apply_property_merge(manifest, actor="pytest", dry_run=False)
        canonical = merged["canonical_property_id"]

        timeline = await build_property_timeline(canonical)
        listing_ids_seen = {l["listing_id"] for l in timeline["listings"]}
        assert listing_ids_seen == {la, lb}
        assert timeline["metrics"]["listing_count"] == 2
        assert timeline["metrics"]["relist_count"] == 1
    finally:
        await _cleanup([la, lb], [p for p in (pa, pb) if p])


@pytest.mark.asyncio
async def test_timeline_after_merge_does_not_double_count_overlapping_dom(db):
    from bot.identity.property_merge import plan_property_merge, apply_property_merge
    from bot.core.property_timeline import build_property_timeline

    la, lb = "__test_pmerge_dom_a__", "__test_pmerge_dom_b__"
    pa = pb = None
    try:
        # [0,10] и [5,15] пересекаются — concurrent_duplicate.
        await _insert_listing(la, address="ДОМ, 1", first_seen=_dt(0), archived_at=_dt(10), last_seen=_dt(10))
        await _insert_listing(lb, address="ДОМ, 1", first_seen=_dt(5), archived_at=_dt(15), last_seen=_dt(15))
        pa = await _make_property("__test_pmerge_dom_hash_a__", first_seen_at=_dt(0), last_seen_at=_dt(10))
        pb = await _make_property("__test_pmerge_dom_hash_b__", first_seen_at=_dt(5), last_seen_at=_dt(15))
        await _link(pa, la); await _link(pb, lb)
        cid = await _make_candidate(la, pb, relationship_type="concurrent_duplicate")
        await _accept(cid)

        plans = await plan_property_merge({pa, pb})
        manifest = _find_component_plan(plans, pa)["manifest"]
        merged = await apply_property_merge(manifest, actor="pytest", dry_run=False)
        canonical = merged["canonical_property_id"]

        timeline = await build_property_timeline(canonical)
        # union [0,15] = 15 дней, НЕ 10+10=20.
        assert timeline["metrics"]["observed_market_days"] == 15.0
    finally:
        await _cleanup([la, lb], [p for p in (pa, pb) if p])


# ── frozen manifest: сохранение/загрузка ────────────────────────────────

@pytest.mark.asyncio
async def test_manifest_save_and_load_roundtrip(db, tmp_path):
    from bot.identity.property_merge import plan_property_merge, save_manifest, load_manifest

    la, lb = "__test_pmerge_manifest_a__", "__test_pmerge_manifest_b__"
    pa = pb = None
    try:
        await _insert_listing(la, address="Манифест, 1", first_seen=_dt(0), last_seen=_dt(5))
        await _insert_listing(lb, address="Манифест, 1", first_seen=_dt(10), last_seen=_dt(15))
        pa = await _make_property("__test_pmerge_manifest_hash_a__", first_seen_at=_dt(0), last_seen_at=_dt(5))
        pb = await _make_property("__test_pmerge_manifest_hash_b__", first_seen_at=_dt(10), last_seen_at=_dt(15))
        await _link(pa, la); await _link(pb, lb)
        cid = await _make_candidate(la, pb, relationship_type="relist")
        await _accept(cid)

        plans = await plan_property_merge({pa, pb})
        manifest = _find_component_plan(plans, pa)["manifest"]

        path = str(tmp_path / "manifest.json")
        save_manifest(manifest, path)
        loaded = load_manifest(path)
        assert loaded["component_hash"] == manifest["component_hash"]
        assert loaded["candidate_ids"] == manifest["candidate_ids"]
    finally:
        await _cleanup([la, lb], [p for p in (pa, pb) if p])


def test_load_manifest_rejects_malformed_shape(tmp_path):
    import json
    from bot.identity.property_merge import load_manifest

    path = str(tmp_path / "bad.json")
    with open(path, "w") as f:
        json.dump({"candidate_ids": [1]}, f)  # missing required keys

    with pytest.raises(ValueError):
        load_manifest(path)
