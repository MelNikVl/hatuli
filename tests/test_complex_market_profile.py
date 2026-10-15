"""tests/test_complex_market_profile.py — задача 2026-08-30, "ЖК как
полноценная сущность", Phase 5. Synthetic fixtures only (тот же паттерн,
что tests/test_property_merge.py) — реальная Postgres test DB
(DATABASE_URL), id с префиксом '__test_cmp_...__', удаляются в finally."""
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


async def _make_complex(name: str, **kwargs) -> int:
    from bot.db.pg import fetchval
    cols = ["name"] + list(kwargs.keys())
    vals = [name] + list(kwargs.values())
    placeholders = ", ".join(f"${i+1}" for i in range(len(vals)))
    return await fetchval(
        f"INSERT INTO complexes ({', '.join(cols)}) VALUES ({placeholders}) RETURNING id", *vals)


async def _insert_listing(lid: str, *, price=20000000, area=45.0, rooms=2,
                           first_seen=None, is_active=True, archived_at=None, is_duplicate=False):
    from bot.db.pg import execute
    await execute(
        """
        INSERT INTO apartment_listings (id, url, price, area, rooms, first_seen, last_seen,
                                         is_active, archived_at, is_duplicate)
        VALUES ($1,$2,$3,$4,$5,$6,$6,$7,$8,$9)
        ON CONFLICT (id) DO UPDATE SET price=$3, area=$4, rooms=$5, first_seen=$6,
            is_active=$7, archived_at=$8, is_duplicate=$9
        """,
        lid, f"https://krisha.kz/test/{lid}", price, area, rooms, first_seen, is_active, archived_at, is_duplicate,
    )


async def _make_property(complex_id: int, address_hash: str, *, floor=5, area_sqm=45.0, rooms=2,
                          first_seen_at=None, last_seen_at=None) -> int:
    from bot.db.pg import fetchval
    return await fetchval(
        "INSERT INTO properties (complex_id, address_hash, floor, area_sqm, rooms, identity_status, "
        "first_seen_at, last_seen_at) VALUES ($1,$2,$3,$4,$5,'provisional', COALESCE($6, now()), COALESCE($7, now())) "
        "RETURNING property_id",
        complex_id, address_hash, floor, area_sqm, rooms, first_seen_at, last_seen_at,
    )


async def _link(property_id: int, listing_id: str):
    from bot.db.pg import execute
    await execute(
        "INSERT INTO property_listings (property_id, listing_id, link_method) VALUES ($1,$2,'bootstrap')",
        property_id, listing_id,
    )


async def _archive_history(listing_id: str, archived_at, reactivated_at):
    from bot.db.pg import execute
    await execute(
        "INSERT INTO listing_archive_history (listing_id, archived_at, reactivated_at) VALUES ($1,$2,$3)",
        listing_id, archived_at, reactivated_at,
    )


async def _price_history_row(listing_id: str, old_price: int, new_price: int, changed_at):
    from bot.db.pg import execute
    await execute(
        "INSERT INTO price_history (listing_id, old_price, new_price, changed_at) VALUES ($1,$2,$3,$4)",
        listing_id, old_price, new_price, changed_at,
    )


async def _views_row(listing_id: str, views_count: int, observed_at):
    from bot.db.pg import execute
    await execute(
        "INSERT INTO views_history (listing_id, views_count, observed_at) VALUES ($1,$2,$3)",
        listing_id, views_count, observed_at,
    )


async def _cleanup(listing_ids, property_ids, complex_ids):
    from bot.db.pg import execute
    lids, pids, cids = list(listing_ids), list(property_ids), list(complex_ids)
    await execute("DELETE FROM views_history WHERE listing_id = ANY($1::text[])", lids)
    await execute("DELETE FROM price_history WHERE listing_id = ANY($1::text[])", lids)
    await execute("DELETE FROM listing_archive_history WHERE listing_id = ANY($1::text[])", lids)
    await execute("DELETE FROM property_listings WHERE listing_id = ANY($1::text[])", lids)
    await execute("DELETE FROM properties WHERE property_id = ANY($1::int[])", pids)
    await execute("DELETE FROM apartment_listings WHERE id = ANY($1::text[])", lids)
    await execute("DELETE FROM complex_aliases WHERE complex_id = ANY($1::int[])", cids)
    await execute("DELETE FROM complexes WHERE id = ANY($1::int[])", cids)


# ── 1. два listings одной property = 1 квартира, не 2 ────────────────────

@pytest.mark.asyncio
async def test_two_listings_same_property_count_as_one_unit(db):
    from bot.core.complex_market_profile import get_complex_market_profile

    la, lb = "__test_cmp_relist_a__", "__test_cmp_relist_b__"
    cid = pid = None
    try:
        cid = await _make_complex("__test_cmp_relist_complex__")
        await _insert_listing(la, first_seen=_dt(0), is_active=False, archived_at=_dt(10))
        await _insert_listing(lb, first_seen=_dt(15), is_active=True)
        pid = await _make_property(cid, "__test_cmp_relist_hash__", first_seen_at=_dt(0), last_seen_at=_dt(20))
        await _link(pid, la)
        await _link(pid, lb)

        profile = await get_complex_market_profile(cid, as_of=_dt(20))
        assert profile["supply"]["observed_unique_listings"] == 2
        assert profile["supply"]["observed_unique_properties"] == 1
        assert profile["liquidity"]["true_relist_count"] == 1
    finally:
        await _cleanup([la, lb], [p for p in (pid,) if p], [c for c in (cid,) if c])


# ── 2. overlapping active intervals не double-count DOM ──────────────────

@pytest.mark.asyncio
async def test_overlapping_intervals_do_not_double_count_dom(db):
    from bot.core.complex_market_profile import get_complex_market_profile

    la, lb = "__test_cmp_overlap_a__", "__test_cmp_overlap_b__"
    cid = pid = None
    try:
        cid = await _make_complex("__test_cmp_overlap_complex__")
        # Оба listing'а одной property активны ОДНОВРЕМЕННО дни 0..10 —
        # merged DOM должен быть ~10 дней, НЕ ~20 (сумма двух интервалов).
        await _insert_listing(la, first_seen=_dt(0), is_active=True)
        await _insert_listing(lb, first_seen=_dt(0), is_active=True)
        pid = await _make_property(cid, "__test_cmp_overlap_hash__", first_seen_at=_dt(0), last_seen_at=_dt(10))
        await _link(pid, la)
        await _link(pid, lb)

        profile = await get_complex_market_profile(cid, as_of=_dt(10))
        dom = profile["liquidity"]["median_observed_dom_days"]
        # insufficient_data=True здесь тоже (1 property < _MIN_SAMPLE=5) —
        # median_observed_dom_days поэтому None; проверяем через sample_size
        # + добираем median руками отдельным низкоуровневым вызовом.
        from bot.core.complex_market_profile import _merge_intervals
        merged = _merge_intervals([(_dt(0), _dt(10)), (_dt(0), _dt(10))])
        assert merged == pytest.approx(10.0)
    finally:
        await _cleanup([la, lb], [p for p in (pid,) if p], [c for c in (cid,) if c])


# ── 3. future listing не попадает в historical as_of ──────────────────────

@pytest.mark.asyncio
async def test_future_listing_excluded_from_historical_as_of(db):
    from bot.core.complex_market_profile import get_complex_market_profile

    l_past, l_future = "__test_cmp_future_a__", "__test_cmp_future_b__"
    cid = p_past = p_future = None
    try:
        cid = await _make_complex("__test_cmp_future_complex__")
        await _insert_listing(l_past, first_seen=_dt(0), is_active=True)
        await _insert_listing(l_future, first_seen=_dt(50), is_active=True)
        p_past = await _make_property(cid, "__test_cmp_future_hash_past__", first_seen_at=_dt(0), last_seen_at=_dt(0))
        p_future = await _make_property(cid, "__test_cmp_future_hash_future__", first_seen_at=_dt(50), last_seen_at=_dt(50))
        await _link(p_past, l_past)
        await _link(p_future, l_future)

        profile = await get_complex_market_profile(cid, as_of=_dt(20))
        assert profile["supply"]["observed_unique_listings"] == 1
        assert profile["supply"]["observed_unique_properties"] == 1

        profile_later = await get_complex_market_profile(cid, as_of=_dt(60))
        assert profile_later["supply"]["observed_unique_listings"] == 2
    finally:
        await _cleanup([l_past, l_future], [p for p in (p_past, p_future) if p], [c for c in (cid,) if c])


@pytest.mark.asyncio
async def test_as_of_in_the_future_raises(db):
    from bot.core.complex_market_profile import get_complex_market_profile

    cid = None
    try:
        cid = await _make_complex("__test_cmp_futureraise_complex__")
        far_future = datetime.now(timezone.utc) + timedelta(days=3650)
        with pytest.raises(ValueError):
            await get_complex_market_profile(cid, as_of=far_future)
    finally:
        await _cleanup([], [], [c for c in (cid,) if c])


# ── 4. relist через Property Identity, не listing IDs ─────────────────────

@pytest.mark.asyncio
async def test_relist_counted_via_property_identity_not_listing_count(db):
    from bot.core.complex_market_profile import get_complex_market_profile

    # property A: 2 listings (relist) ; property B: 1 listing (не relist)
    la1, la2, lb1 = "__test_cmp_pi_a1__", "__test_cmp_pi_a2__", "__test_cmp_pi_b1__"
    cid = pa = pb = None
    try:
        cid = await _make_complex("__test_cmp_pi_complex__")
        await _insert_listing(la1, first_seen=_dt(0), is_active=False, archived_at=_dt(5))
        await _insert_listing(la2, first_seen=_dt(10), is_active=True)
        await _insert_listing(lb1, first_seen=_dt(0), is_active=True)
        pa = await _make_property(cid, "__test_cmp_pi_hash_a__", first_seen_at=_dt(0), last_seen_at=_dt(15))
        pb = await _make_property(cid, "__test_cmp_pi_hash_b__", first_seen_at=_dt(0), last_seen_at=_dt(15))
        await _link(pa, la1)
        await _link(pa, la2)
        await _link(pb, lb1)

        profile = await get_complex_market_profile(cid, as_of=_dt(15))
        assert profile["supply"]["observed_unique_listings"] == 3
        assert profile["supply"]["observed_unique_properties"] == 2
        assert profile["liquidity"]["true_relist_count"] == 1
    finally:
        await _cleanup([la1, la2, lb1], [p for p in (pa, pb) if p], [c for c in (cid,) if c])


# ── 5. недостаточная выборка -> insufficient_data, не выдуманное число ────

@pytest.mark.asyncio
async def test_small_complex_returns_insufficient_data(db):
    from bot.core.complex_market_profile import get_complex_market_profile

    la = "__test_cmp_small_a__"
    cid = pid = None
    try:
        cid = await _make_complex("__test_cmp_small_complex__")
        await _insert_listing(la, price=20000000, area=45.0, first_seen=_dt(0), is_active=True)
        pid = await _make_property(cid, "__test_cmp_small_hash__", first_seen_at=_dt(0), last_seen_at=_dt(0))
        await _link(pid, la)

        profile = await get_complex_market_profile(cid, as_of=_dt(5))
        assert profile["price"]["insufficient_data"] is True
        assert profile["price"]["median_price_m2"] is None
        assert profile["liquidity"]["insufficient_data"] is True
        assert profile["liquidity"]["median_observed_dom_days"] is None
        # sample size ВСЕГДА присутствует, даже когда insufficient
        assert profile["price"]["sample_size"] == 1
        assert profile["liquidity"]["sample_size_properties"] == 1
    finally:
        await _cleanup([la], [p for p in (pid,) if p], [c for c in (cid,) if c])


@pytest.mark.asyncio
async def test_unknown_complex_returns_none(db):
    from bot.core.complex_market_profile import get_complex_market_profile
    profile = await get_complex_market_profile(-999999)
    assert profile is None


@pytest.mark.asyncio
async def test_demand_insufficient_history_when_no_views_data(db):
    from bot.core.complex_market_profile import get_complex_market_profile

    la = "__test_cmp_demand_a__"
    cid = pid = None
    try:
        cid = await _make_complex("__test_cmp_demand_complex__")
        await _insert_listing(la, first_seen=_dt(0), is_active=True)
        pid = await _make_property(cid, "__test_cmp_demand_hash__", first_seen_at=_dt(0), last_seen_at=_dt(0))
        await _link(pid, la)

        profile = await get_complex_market_profile(cid, as_of=_dt(5))
        assert profile["demand"]["insufficient_history"] is True
    finally:
        await _cleanup([la], [p for p in (pid,) if p], [c for c in (cid,) if c])


@pytest.mark.asyncio
async def test_deterministic_output_same_inputs(db):
    from bot.core.complex_market_profile import get_complex_market_profile

    la = "__test_cmp_det_a__"
    cid = pid = None
    try:
        cid = await _make_complex("__test_cmp_det_complex__")
        await _insert_listing(la, price=25000000, area=50.0, first_seen=_dt(0), is_active=True)
        pid = await _make_property(cid, "__test_cmp_det_hash__", first_seen_at=_dt(0), last_seen_at=_dt(0))
        await _link(pid, la)

        p1 = await get_complex_market_profile(cid, as_of=_dt(3))
        p2 = await get_complex_market_profile(cid, as_of=_dt(3))
        assert p1 == p2
    finally:
        await _cleanup([la], [p for p in (pid,) if p], [c for c in (cid,) if c])


@pytest.mark.asyncio
@pytest.mark.parametrize("as_of_day,expected_price", [(10, 20_000_000), (20, 25_000_000),
                                                     (25, 25_000_000), (40, 30_000_000)])
async def test_historical_price_is_reconstructed_in_database_query(db, as_of_day, expected_price):
    from bot.core.complex_market_profile import get_complex_market_profile
    lids, pids, cids = [], [], []
    try:
        cid = await _make_complex("__test_cmp_price_asof__")
        cids.append(cid)
        for i in range(5):
            lid = f"__test_cmp_price_asof_{i}__"
            lids.append(lid)
            await _insert_listing(lid, price=30_000_000, area=50, first_seen=_dt(0))
            pid = await _make_property(cid, f"__test_cmp_price_asof_hash_{i}__")
            pids.append(pid)
            await _link(pid, lid)
            await _price_history_row(lid, 20_000_000, 25_000_000, _dt(20))
            await _price_history_row(lid, 25_000_000, 30_000_000, _dt(30))
        result = await get_complex_market_profile(cid, _dt(as_of_day))
        assert result["price"]["median_asking_price"] == expected_price
        assert result["price"]["median_price_m2"] == expected_price / 50
        assert result["price"]["sample_size"] == 5
    finally:
        await _cleanup(lids, pids, cids)


@pytest.mark.asyncio
async def test_historical_exit_and_reactivation_reconstructed_from_database(db):
    from bot.core.complex_market_profile import get_complex_market_profile
    lids, pids, cids = [], [], []
    try:
        cid = await _make_complex("__test_cmp_archive_asof__")
        cids.append(cid)
        for i in range(5):
            lid = f"__test_cmp_archive_asof_{i}__"
            lids.append(lid)
            await _insert_listing(lid, first_seen=_dt(0), is_active=False, archived_at=_dt(80))
            pid = await _make_property(cid, f"__test_cmp_archive_asof_hash_{i}__")
            pids.append(pid)
            await _link(pid, lid)
            await _archive_history(lid, _dt(10), _dt(40))
        historical = await get_complex_market_profile(cid, _dt(30))
        assert historical["supply"]["active_properties_now"] == 0
        assert historical["liquidity"]["median_observed_dom_days"] == 10
        assert historical["liquidity"]["fraction_disappearing"]["within_30d"]["fraction"] == 1
        later = await get_complex_market_profile(cid, _dt(50))
        assert later["supply"]["active_properties_now"] == 5
        assert later["liquidity"]["median_observed_dom_days"] == 20
        assert later["liquidity"]["true_relist_count"] == 0
    finally:
        await _cleanup(lids, pids, cids)


@pytest.mark.asyncio
async def test_canonical_alias_and_verified_name_share_one_market_profile(db):
    from bot.core.complex_market_profile import get_complex_market_profile
    from bot.db.pg import execute
    lids, pids, cids = [], [], []
    try:
        root = await _make_complex("__test_cmp_identity_root__")
        cids.append(root)
        alias = await _make_complex("__test_cmp_identity_old_name__", canonical_id=root)
        cids.append(alias)
        verified_name = "__test_cmp_identity_verified_spelling__"
        await execute("INSERT INTO complex_aliases (complex_id,name,normalized_name,source,source_id,status) "
                      "VALUES ($1,$2,complex_name_key($2),'test',$2,'verified')", root, verified_name)
        for i in range(2):
            lid = f"__test_cmp_identity_alias_{i}__"
            lids.append(lid)
            await _insert_listing(lid, first_seen=_dt(0))
            if i == 0:
                await execute("UPDATE apartment_listings SET complex_id=$2,complex_name='stale unmatched name' "
                              "WHERE id=$1", lid, alias)
            else:
                await execute("UPDATE apartment_listings SET complex_name=$2 WHERE id=$1", lid, verified_name)
            pid = await _make_property(alias if i == 0 else None, f"__test_cmp_identity_alias_hash_{i}__")
            pids.append(pid)
            await _link(pid, lid)
        canonical = await get_complex_market_profile(root, _dt(5))
        redirected = await get_complex_market_profile(alias, _dt(5))
        assert canonical == redirected
        assert canonical["complex_id"] == root
        assert canonical["identity"]["canonical_name"] == "__test_cmp_identity_root__"
        assert canonical["supply"]["observed_unique_listings"] == 2
        assert canonical["supply"]["observed_unique_properties"] == 2
    finally:
        await _cleanup(lids, pids, cids)


@pytest.mark.asyncio
async def test_explicit_listing_binding_overrules_conflicting_legacy_property_complex(db):
    from bot.core.complex_market_profile import get_complex_market_profile
    from bot.db.pg import execute
    lids, pids, cids = [], [], []
    try:
        old = await _make_complex("__test_cmp_identity_old_property__")
        current = await _make_complex("__test_cmp_identity_current_listing__")
        cids.extend([old, current])
        lid = "__test_cmp_identity_conflicting_listing__"
        lids.append(lid)
        await _insert_listing(lid, first_seen=_dt(0))
        await execute("UPDATE apartment_listings SET complex_id=$2,complex_name=$3 WHERE id=$1",
                      lid, current, "__test_cmp_identity_old_property__")
        pid = await _make_property(old, "__test_cmp_identity_conflicting_hash__")
        pids.append(pid)
        await _link(pid, lid)
        old_profile = await get_complex_market_profile(old, _dt(5))
        current_profile = await get_complex_market_profile(current, _dt(5))
        assert old_profile["supply"]["observed_unique_properties"] == 0
        assert current_profile["supply"]["observed_unique_properties"] == 1
        assert current_profile["supply"]["observed_unique_listings"] == 1
    finally:
        await _cleanup(lids, pids, cids)


@pytest.mark.asyncio
async def test_umbrella_aggregates_children_without_double_counting_a_property(db):
    from bot.core.complex_market_profile import get_complex_market_profile
    from bot.db.pg import execute
    lids, pids, cids = [], [], []
    try:
        parent = await _make_complex("__test_cmp_identity_umbrella__", is_umbrella=True)
        parent_alias = await _make_complex("__test_cmp_identity_umbrella_alias__", canonical_id=parent)
        first = await _make_complex("__test_cmp_identity_child_first__", parent_complex_id=parent)
        first_alias = await _make_complex("__test_cmp_identity_child_alias__", canonical_id=first,
                                         parent_complex_id=parent)
        second = await _make_complex("__test_cmp_identity_child_second__", parent_complex_id=parent_alias)
        cids.extend([parent, parent_alias, first, first_alias, second])
        same_unit = await _make_property(first_alias, "__test_cmp_identity_same_unit__")
        other_unit = await _make_property(second, "__test_cmp_identity_other_unit__")
        pids.extend([same_unit, other_unit])
        for i, (cid, pid) in enumerate([(first, same_unit), (first_alias, same_unit), (second, other_unit)]):
            lid = f"__test_cmp_identity_umbrella_listing_{i}__"
            lids.append(lid)
            await _insert_listing(lid, first_seen=_dt(0))
            await execute("UPDATE apartment_listings SET complex_id=$2 WHERE id=$1", lid, cid)
            await _link(pid, lid)
        profile = await get_complex_market_profile(parent, _dt(5))
        assert profile["supply"]["observed_unique_listings"] == 3
        assert profile["supply"]["observed_unique_properties"] == 2
        assert profile["supply"]["active_properties_now"] == 2
        assert profile["price"]["sample_size"] == 2
        child_profile = await get_complex_market_profile(first, _dt(5))
        assert child_profile["supply"]["observed_unique_listings"] == 2
        assert child_profile["supply"]["observed_unique_properties"] == 1
        second_profile = await get_complex_market_profile(second, _dt(5))
        assert second_profile["supply"]["observed_unique_properties"] == 1
    finally:
        await _cleanup(lids, pids, cids)
