"""tests/test_property_timeline.py — Property Timeline, Phase 1 (задача
2026-08-20, "Property Timeline как первый продуктовый слой поверх Property
Identity"). Synthetic fixtures only (тот же паттерн, что tests/test_
property_match_review.py) — реальная Postgres test DB (DATABASE_URL),
никакой зависимости от прод-данных: все id с префиксом '__test_ptl_...__',
удаляются в finally."""
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


async def _insert_listing(lid, *, address="Тест, 1", floor=5, area=45.0, rooms=2, price=None,
                           seller_name=None, is_active=True, first_seen=None, last_seen=None,
                           archived_at=None, archive_reason=None):
    from bot.db.pg import execute
    await execute(
        """
        INSERT INTO apartment_listings (id, url, address, floor, area, rooms, price, seller_name,
                                         is_active, first_seen, last_seen, archived_at, archive_reason)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
        ON CONFLICT (id) DO UPDATE SET address=$3, floor=$4, area=$5, rooms=$6, price=$7,
            seller_name=$8, is_active=$9, first_seen=$10, last_seen=$11, archived_at=$12, archive_reason=$13
        """,
        lid, f"https://krisha.kz/test/{lid}", address, floor, area, rooms, price, seller_name,
        is_active, first_seen, last_seen, archived_at, archive_reason,
    )


async def _make_property(address_hash, *, floor=5, area_sqm=45.0, rooms=2, identity_status="provisional"):
    from bot.db.pg import fetchval
    return await fetchval(
        "INSERT INTO properties (address_hash, floor, area_sqm, rooms, identity_status) "
        "VALUES ($1,$2,$3,$4,$5) RETURNING property_id",
        address_hash, floor, area_sqm, rooms, identity_status,
    )


async def _link(property_id, listing_id, *, link_method="bootstrap", confidence=1.0, linked_at=None):
    from bot.db.pg import execute
    await execute(
        "INSERT INTO property_listings (property_id, listing_id, link_method, confidence, linked_at) "
        "VALUES ($1,$2,$3,$4, COALESCE($5, now()))",
        property_id, listing_id, link_method, confidence, linked_at,
    )


async def _price_change(listing_id, old_price, new_price, changed_at):
    from bot.db.pg import execute
    await execute(
        "INSERT INTO price_history (listing_id, old_price, new_price, changed_at) VALUES ($1,$2,$3,$4)",
        listing_id, old_price, new_price, changed_at,
    )


async def _archive_cycle(listing_id, archived_at, archive_reason, reactivated_at):
    from bot.db.pg import execute
    await execute(
        "INSERT INTO listing_archive_history (listing_id, archived_at, archive_reason, reactivated_at) "
        "VALUES ($1,$2,$3,$4)",
        listing_id, archived_at, archive_reason, reactivated_at,
    )


async def _cleanup(listing_ids, property_ids):
    from bot.db.pg import execute
    lids, pids = list(listing_ids), list(property_ids)
    await execute(
        "DELETE FROM property_candidate_photo_evidence WHERE candidate_id IN "
        "(SELECT candidate_id FROM property_match_candidates "
        " WHERE listing_id = ANY($1::text[]) OR candidate_property_id = ANY($2::int[]))",
        lids, pids,
    )
    await execute(
        "DELETE FROM property_match_candidates WHERE listing_id = ANY($1::text[]) "
        "OR candidate_property_id = ANY($2::int[])", lids, pids,
    )
    await execute("DELETE FROM price_history WHERE listing_id = ANY($1::text[])", lids)
    await execute("DELETE FROM listing_archive_history WHERE listing_id = ANY($1::text[])", lids)
    await execute("DELETE FROM property_listings WHERE listing_id = ANY($1::text[])", lids)
    await execute("DELETE FROM properties WHERE property_id = ANY($1::int[])", pids)
    await execute("DELETE FROM apartment_listings WHERE id = ANY($1::text[])", lids)


# ── property не существует ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_property_not_found_returns_none(db):
    from bot.core.property_timeline import build_property_timeline
    result = await build_property_timeline(-999999)
    assert result is None


# ── property с одним listing ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_single_listing_basic_shape_and_metrics(db):
    from bot.core.property_timeline import build_property_timeline
    lid = "__test_ptl_single_a__"
    pid = None
    try:
        await _insert_listing(lid, price=100, first_seen=_dt(0), last_seen=_dt(5))
        pid = await _make_property("__test_ptl_hash_single__")
        await _link(pid, lid, linked_at=_dt(0))

        result = await build_property_timeline(pid)
        assert result["property_id"] == pid
        assert result["identity_status"] == "provisional"
        assert result["confidence"] == 1.0

        m = result["metrics"]
        assert m["listing_count"] == 1
        assert m["relist_count"] == 0
        assert m["first_seen_at"] == _dt(0).isoformat()
        assert m["last_seen_at"] == _dt(5).isoformat()
        assert m["observed_span_days"] == 5.0
        assert m["initial_price"] == 100
        assert m["latest_price"] == 100
        assert m["price_change_count"] == 0
        assert m["observed_market_days"] == 5.0
        assert m["unique_observed_seller_names"] == 0

        assert len(result["listings"]) == 1
        assert result["listings"][0]["listing_id"] == lid

        types = {e["type"] for e in result["events"]}
        assert "property_identity_link" in types
        assert "new_listing_linked" in types
        assert "listing_first_seen" in types
        assert "listing_relist" not in types
    finally:
        await _cleanup([lid], [pid] if pid else [])


# ── несколько последовательных relist ────────────────────────────────

@pytest.mark.asyncio
async def test_sequential_relists_produce_relist_events_and_count(db):
    from bot.core.property_timeline import build_property_timeline
    l1, l2, l3 = "__test_ptl_relist_a__", "__test_ptl_relist_b__", "__test_ptl_relist_c__"
    pid = None
    try:
        await _insert_listing(l1, first_seen=_dt(0), archived_at=_dt(10), last_seen=_dt(10))
        await _insert_listing(l2, first_seen=_dt(15), archived_at=_dt(25), last_seen=_dt(25))
        await _insert_listing(l3, first_seen=_dt(30), last_seen=_dt(35))
        pid = await _make_property("__test_ptl_hash_relist__")
        await _link(pid, l1)
        await _link(pid, l2)
        await _link(pid, l3)

        result = await build_property_timeline(pid)
        assert result["metrics"]["listing_count"] == 3
        assert result["metrics"]["relist_count"] == 2

        relists = [e for e in result["events"] if e["type"] == "listing_relist"]
        assert len(relists) == 2
        assert relists[0]["listing_id"] == l2 and relists[0]["before"] == l1
        assert relists[1]["listing_id"] == l3 and relists[1]["before"] == l2

        new_linked = [e for e in result["events"] if e["type"] == "new_listing_linked"]
        assert len(new_linked) == 1
        assert new_linked[0]["listing_id"] == l1
    finally:
        await _cleanup([l1, l2, l3], [pid] if pid else [])


# ── два одновременно активных listings -> DOM не удваивается ─────────

@pytest.mark.asyncio
async def test_concurrent_listings_do_not_double_observed_market_days(db):
    from bot.core.property_timeline import build_property_timeline
    l1, l2 = "__test_ptl_concurrent_a__", "__test_ptl_concurrent_b__"
    pid = None
    try:
        # [0,10] и [5,15] пересекаются -> union = [0,15] = 15 дней, НЕ 10+10=20.
        await _insert_listing(l1, first_seen=_dt(0), archived_at=_dt(10), last_seen=_dt(10))
        await _insert_listing(l2, first_seen=_dt(5), archived_at=_dt(15), last_seen=_dt(15))
        pid = await _make_property("__test_ptl_hash_concurrent__")
        await _link(pid, l1)
        await _link(pid, l2)

        result = await build_property_timeline(pid)
        assert result["metrics"]["observed_market_days"] == 15.0
    finally:
        await _cleanup([l1, l2], [pid] if pid else [])


@pytest.mark.asyncio
async def test_non_overlapping_listings_sum_normally(db):
    """Контрольный случай: НЕ пересекающиеся интервалы -> сумма (не union
    в один большой интервал) — доказывает, что _merge_intervals не
    схлопывает то, что не пересекается."""
    from bot.core.property_timeline import build_property_timeline
    l1, l2 = "__test_ptl_gap_a__", "__test_ptl_gap_b__"
    pid = None
    try:
        await _insert_listing(l1, first_seen=_dt(0), archived_at=_dt(10), last_seen=_dt(10))
        await _insert_listing(l2, first_seen=_dt(20), archived_at=_dt(25), last_seen=_dt(25))
        pid = await _make_property("__test_ptl_hash_gap__")
        await _link(pid, l1)
        await _link(pid, l2)

        result = await build_property_timeline(pid)
        assert result["metrics"]["observed_market_days"] == 15.0  # 10 + 5, разрыв 10-20 не считается
    finally:
        await _cleanup([l1, l2], [pid] if pid else [])


# ── несколько price changes ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_multiple_price_changes_tracked_in_trajectory(db):
    from bot.core.property_timeline import build_property_timeline
    lid = "__test_ptl_price_a__"
    pid = None
    try:
        await _insert_listing(lid, price=80, first_seen=_dt(0), last_seen=_dt(10))
        pid = await _make_property("__test_ptl_hash_price__")
        await _link(pid, lid)
        await _price_change(lid, 100, 90, _dt(3))
        await _price_change(lid, 90, 80, _dt(7))

        result = await build_property_timeline(pid)
        m = result["metrics"]
        assert m["price_change_count"] == 2
        assert m["initial_price"] == 100  # старейшая известная цена — old_price первого изменения
        assert m["latest_price"] == 80
        assert m["min_price"] == 80
        assert m["max_price"] == 100
        assert m["total_price_change_pct"] == -20.0

        price_events = sorted((e for e in result["events"] if e["type"] == "price_change"),
                               key=lambda e: e["timestamp"])
        assert len(price_events) == 2
        assert price_events[0]["before"] == 100 and price_events[0]["after"] == 90
        assert price_events[1]["before"] == 90 and price_events[1]["after"] == 80
    finally:
        await _cleanup([lid], [pid] if pid else [])


# ── seller_name: одинаковый в двух listings ───────────────────────────

@pytest.mark.asyncio
async def test_same_seller_name_across_listings_no_change_event(db):
    from bot.core.property_timeline import build_property_timeline
    l1, l2 = "__test_ptl_seller_same_a__", "__test_ptl_seller_same_b__"
    pid = None
    try:
        await _insert_listing(l1, seller_name="Иван Иванов", first_seen=_dt(0), archived_at=_dt(5), last_seen=_dt(5))
        await _insert_listing(l2, seller_name="  иван   иванов ", first_seen=_dt(10), last_seen=_dt(15))
        pid = await _make_property("__test_ptl_hash_seller_same__")
        await _link(pid, l1)
        await _link(pid, l2)

        result = await build_property_timeline(pid)
        assert result["metrics"]["unique_observed_seller_names"] == 1
        assert not [e for e in result["events"] if e["type"] == "seller_observed_change"]
    finally:
        await _cleanup([l1, l2], [pid] if pid else [])


# ── seller_name: разные в двух listings ────────────────────────────────

@pytest.mark.asyncio
async def test_different_seller_names_produce_change_event(db):
    from bot.core.property_timeline import build_property_timeline
    l1, l2 = "__test_ptl_seller_diff_a__", "__test_ptl_seller_diff_b__"
    pid = None
    try:
        await _insert_listing(l1, seller_name="Иван Иванов", first_seen=_dt(0), archived_at=_dt(5), last_seen=_dt(5))
        await _insert_listing(l2, seller_name="Пётр Петров", first_seen=_dt(10), last_seen=_dt(15))
        pid = await _make_property("__test_ptl_hash_seller_diff__")
        await _link(pid, l1)
        await _link(pid, l2)

        result = await build_property_timeline(pid)
        assert result["metrics"]["unique_observed_seller_names"] == 2
        changes = [e for e in result["events"] if e["type"] == "seller_observed_change"]
        assert len(changes) == 1
        assert changes[0]["before"] == "Иван Иванов"
        assert changes[0]["after"] == "Пётр Петров"
        assert changes[0]["listing_id"] == l2
    finally:
        await _cleanup([l1, l2], [pid] if pid else [])


# ── archived -> reactivated ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_archived_then_reactivated_listing(db):
    from bot.core.property_timeline import build_property_timeline
    lid = "__test_ptl_reactivate_a__"
    pid = None
    try:
        # Реактивирован СЕЙЧАС (archived_at очищен, is_active=True) — старый
        # период архивации живёт ТОЛЬКО в listing_archive_history.
        await _insert_listing(lid, first_seen=_dt(0), last_seen=_dt(20), archived_at=None, is_active=True)
        await _archive_cycle(lid, archived_at=_dt(5), archive_reason="archived_badge", reactivated_at=_dt(8))
        pid = await _make_property("__test_ptl_hash_reactivate__")
        await _link(pid, lid)

        result = await build_property_timeline(pid)
        reactivated = [e for e in result["events"] if e["type"] == "listing_reactivated"]
        assert len(reactivated) == 1
        assert reactivated[0]["timestamp"] == _dt(8).isoformat()
        assert reactivated[0]["before"]["archive_reason"] == "archived_badge"

        archived = [e for e in result["events"] if e["type"] == "listing_archived"]
        assert archived == []  # СЕЙЧАС не архивирован — нет текущего archived_at

        # observed_market_days: [0,5] + [8,20] = 5 + 12 = 17 (разрыв 5-8 не считается)
        assert result["metrics"]["observed_market_days"] == 17.0
    finally:
        await _cleanup([lid], [pid] if pid else [])


# ── provisional identity ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_provisional_identity_status_surfaced_honestly(db):
    from bot.core.property_timeline import build_property_timeline
    lid = "__test_ptl_provisional_a__"
    pid = None
    try:
        await _insert_listing(lid, first_seen=_dt(0), last_seen=_dt(1))
        pid = await _make_property("__test_ptl_hash_provisional__", identity_status="provisional")
        await _link(pid, lid, link_method="bootstrap", confidence=1.0)

        result = await build_property_timeline(pid)
        assert result["identity_status"] == "provisional"
        assert result["identity"]["status"] == "provisional"
        assert result["identity"]["linked_listing_count"] == 1
        assert set(result["identity"]["candidate_counts"]) == {"pending", "accepted", "rejected"}
        assert result["identity"]["photo_evidence_available"] is False
    finally:
        await _cleanup([lid], [pid] if pid else [])


@pytest.mark.asyncio
async def test_identity_confidence_is_weakest_link_not_average(db):
    """confidence на уровне property = MIN confidence связей (задача,
    п.5: provisional property не должна выглядеть подтверждённой) — не
    среднее, иначе одна сильная связь маскировала бы одну слабую."""
    from bot.core.property_timeline import build_property_timeline
    l1, l2 = "__test_ptl_conf_a__", "__test_ptl_conf_b__"
    pid = None
    try:
        await _insert_listing(l1, first_seen=_dt(0), archived_at=_dt(5), last_seen=_dt(5))
        await _insert_listing(l2, first_seen=_dt(10), last_seen=_dt(15))
        pid = await _make_property("__test_ptl_hash_conf__")
        await _link(pid, l1, link_method="bootstrap", confidence=1.0)
        await _link(pid, l2, link_method="fuzzy", confidence=0.65)

        result = await build_property_timeline(pid)
        assert result["confidence"] == 0.65
        assert result["identity"]["confidence"] == 0.65
    finally:
        await _cleanup([l1, l2], [pid] if pid else [])


# ── photo evidence отсутствует ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_photo_evidence_no_event_and_flag_false(db):
    from bot.core.property_timeline import build_property_timeline
    lid = "__test_ptl_nophoto_a__"
    pid = None
    try:
        await _insert_listing(lid, first_seen=_dt(0), last_seen=_dt(1))
        pid = await _make_property("__test_ptl_hash_nophoto__")
        await _link(pid, lid)

        result = await build_property_timeline(pid)
        assert result["identity"]["photo_evidence_available"] is False
        assert not [e for e in result["events"] if e["type"] == "photo_evidence_observed"]
    finally:
        await _cleanup([lid], [pid] if pid else [])


@pytest.mark.asyncio
async def test_photo_evidence_present_surfaces_event_not_raw_rows(db):
    """Положительный путь — доказывает, что JOIN на property_match_
    candidates/property_candidate_photo_evidence реально работает, не
    только что 'отсутствие' корректно даёт False."""
    from bot.core.property_timeline import build_property_timeline
    from bot.db.pg import execute, fetchval

    lid = "__test_ptl_photo_a__"
    other_lid = "__test_ptl_photo_other__"
    pid = None
    other_pid = None
    candidate_id = None
    try:
        await _insert_listing(lid, first_seen=_dt(0), last_seen=_dt(1))
        await _insert_listing(other_lid, first_seen=_dt(0), last_seen=_dt(1))
        pid = await _make_property("__test_ptl_hash_photo__")
        other_pid = await _make_property("__test_ptl_hash_photo_other__")
        await _link(pid, lid)
        await _link(other_pid, other_lid)

        candidate_id = await fetchval(
            """
            INSERT INTO property_match_candidates
                (listing_id, candidate_property_id, match_method, match_score, matcher_version, status)
            VALUES ($1, $2, 'exact_hash', 0.9, 'test_v1', 'pending') RETURNING candidate_id
            """,
            other_lid, pid,
        )
        await execute(
            """
            INSERT INTO property_candidate_photo_evidence
                (candidate_id, exact_shared_count, shared_unit_specific_count, model_version, processing_status)
            VALUES ($1, 2, 1, 'test_v1', 'ok')
            """,
            candidate_id,
        )

        result = await build_property_timeline(pid)
        assert result["identity"]["photo_evidence_available"] is True
        photo_events = [e for e in result["events"] if e["type"] == "photo_evidence_observed"]
        assert len(photo_events) == 1
        assert photo_events[0]["listing_id"] == other_lid
        assert photo_events[0]["evidence"]["exact_shared_count"] == 2
        assert photo_events[0]["evidence"]["candidate_id"] == candidate_id
    finally:
        await _cleanup([lid, other_lid], [p for p in (pid, other_pid) if p])


# ── детерминированный порядок events ───────────────────────────────────

@pytest.mark.asyncio
async def test_events_order_is_deterministic_across_repeated_calls(db):
    from bot.core.property_timeline import build_property_timeline
    l1, l2 = "__test_ptl_det_a__", "__test_ptl_det_b__"
    pid = None
    try:
        await _insert_listing(l1, first_seen=_dt(0), archived_at=_dt(5), last_seen=_dt(5), seller_name="A")
        await _insert_listing(l2, first_seen=_dt(10), last_seen=_dt(15), seller_name="B")
        pid = await _make_property("__test_ptl_hash_det__")
        await _link(pid, l1)
        await _link(pid, l2)
        await _price_change(l1, 100, 90, _dt(2))

        r1 = await build_property_timeline(pid)
        r2 = await build_property_timeline(pid)
        assert r1["events"] == r2["events"]
        # Порядок event'ов внутри одного timestamp — стабильный, не
        # зависящий от порядка чтения из БД.
        timestamps = [e["timestamp"] for e in r1["events"]]
        assert timestamps == sorted(timestamps)
    finally:
        await _cleanup([l1, l2], [pid] if pid else [])


@pytest.mark.asyncio
async def test_tied_timestamp_events_use_fixed_type_priority(db):
    """property_identity_link/new_listing_linked/listing_first_seen на
    ТОЧНО ОДИНАКОВОМ timestamp (linked_at == first_seen намеренно) —
    порядок определяется _EVENT_TYPE_ORDER, не случайностью запроса."""
    from bot.core.property_timeline import build_property_timeline
    lid = "__test_ptl_tie_a__"
    pid = None
    try:
        await _insert_listing(lid, first_seen=_dt(0), last_seen=_dt(1))
        pid = await _make_property("__test_ptl_hash_tie__")
        await _link(pid, lid, linked_at=_dt(0))  # та же timestamp, что first_seen

        result = await build_property_timeline(pid)
        same_ts = [e for e in result["events"] if e["timestamp"] == _dt(0).isoformat()]
        types_in_order = [e["type"] for e in same_ts]
        assert types_in_order.index("property_identity_link") < types_in_order.index("new_listing_linked")
        assert types_in_order.index("new_listing_linked") < types_in_order.index("listing_first_seen")
    finally:
        await _cleanup([lid], [pid] if pid else [])
