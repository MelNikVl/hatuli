"""Регрессия для Фазы A.5, п.1 вердикт-стратегии (docs/verdict_strategy.md,
задача 2026-08-14): bot/core/bargain.get_comparables() раньше не
фильтровал активность НИ В ОДНОМ из 4 SQL-запросов — архивные
(проданные/снятые) объявления тихо участвовали в медиане "текущего
рынка". Теперь: is_active IS NOT FALSE по умолчанию, либо точечный
as_of-срез (first_seen<=as_of<=archived_at), если передан. Self-exclusion
(exclude_id) уже был корректен ДО этой задачи — тест защищает от
регресса, не чинит новый баг.

Реальная БД (тот же паттерн, что tests/test_effective_score.py) —
get_comparables() делает живые SQL-запросы, не чистая функция."""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

_DISTRICT = "__Тестовый Аналоги р-н__"


@pytest_asyncio.fixture
async def db():
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    yield
    await close_pool()


async def _insert(id_, price, is_active=True, archived_at=None, first_seen=None,
                   rooms=2, area=60.0, district=_DISTRICT):
    from bot.db.pg import execute
    await execute(
        """
        INSERT INTO apartment_listings
            (id, price, area, rooms, district, is_active, archived_at, first_seen)
        VALUES ($1, $2, $3, $4, $5, $6, $7, COALESCE($8, now()))
        """,
        id_, price, area, rooms, district, is_active, archived_at, first_seen,
    )


async def _cleanup(*ids):
    from bot.db.pg import execute
    await execute("DELETE FROM apartment_listings WHERE id = ANY($1::text[])", list(ids))


@pytest.mark.asyncio
async def test_archived_analog_excluded_by_default(db):
    from bot.core.bargain import get_comparables
    active_id, archived_id = "__test_cmp_active__", "__test_cmp_archived__"
    await _insert(active_id, price=30_000_000, is_active=True)
    await _insert(archived_id, price=10_000_000, is_active=False,
                  archived_at=datetime.now(timezone.utc))
    try:
        comps, meta = await get_comparables(
            lat=None, lon=None, rooms=2, area=60.0, current_price=30_000_000,
            district=_DISTRICT,
        )
        ids = {c["id"] for c in comps}
        assert active_id in ids
        assert archived_id not in ids
    finally:
        await _cleanup(active_id, archived_id)


@pytest.mark.asyncio
async def test_self_listing_excluded_from_analogs(db):
    from bot.core.bargain import get_comparables
    self_id, other_id = "__test_cmp_self__", "__test_cmp_other__"
    await _insert(self_id, price=30_000_000, is_active=True)
    await _insert(other_id, price=31_000_000, is_active=True)
    try:
        comps, meta = await get_comparables(
            lat=None, lon=None, rooms=2, area=60.0, current_price=30_000_000,
            district=_DISTRICT, exclude_id=self_id,
        )
        ids = {c["id"] for c in comps}
        assert self_id not in ids
        assert other_id in ids
    finally:
        await _cleanup(self_id, other_id)


@pytest.mark.asyncio
async def test_active_only_selection_matches_is_active_flag(db):
    from bot.core.bargain import get_comparables
    ids = [f"__test_cmp_mix_{i}__" for i in range(4)]
    await _insert(ids[0], price=30_000_000, is_active=True)
    await _insert(ids[1], price=31_000_000, is_active=True)
    await _insert(ids[2], price=10_000_000, is_active=False, archived_at=datetime.now(timezone.utc))
    await _insert(ids[3], price=11_000_000, is_active=False, archived_at=datetime.now(timezone.utc))
    try:
        comps, meta = await get_comparables(
            lat=None, lon=None, rooms=2, area=60.0, current_price=30_000_000,
            district=_DISTRICT,
        )
        got = {c["id"] for c in comps}
        assert got == {ids[0], ids[1]}
    finally:
        await _cleanup(*ids)


@pytest.mark.asyncio
async def test_as_of_reconstructs_point_in_time_active_set(db):
    from bot.core.bargain import get_comparables
    now = datetime.now(timezone.utc)
    # A: жило до и после as_of -> должно попасть.
    # B: появилось ПОСЛЕ as_of (first_seen в будущем относительно as_of) -> не должно.
    # C: уже архивировано ДО as_of (архивная точка раньше as_of) -> не должно.
    id_a, id_b, id_c = "__test_asof_a__", "__test_asof_b__", "__test_asof_c__"
    as_of = now - timedelta(days=10)
    await _insert(id_a, price=30_000_000, is_active=False,
                  first_seen=now - timedelta(days=20), archived_at=now - timedelta(days=1))
    await _insert(id_b, price=31_000_000, is_active=True,
                  first_seen=now - timedelta(days=5))
    await _insert(id_c, price=10_000_000, is_active=False,
                  first_seen=now - timedelta(days=30), archived_at=now - timedelta(days=15))
    try:
        comps, meta = await get_comparables(
            lat=None, lon=None, rooms=2, area=60.0, current_price=30_000_000,
            district=_DISTRICT, as_of=as_of,
        )
        got = {c["id"] for c in comps}
        assert id_a in got
        assert id_b not in got
        assert id_c not in got
    finally:
        await _cleanup(id_a, id_b, id_c)


@pytest.mark.asyncio
async def test_as_of_none_preserves_current_behavior(db):
    # Регресс на дефолт: as_of не передан -> тот же результат, что явный
    # is_active IS NOT FALSE (не ломает существующих вызывающих).
    from bot.core.bargain import get_comparables
    active_id, archived_id = "__test_asof_none_active__", "__test_asof_none_archived__"
    await _insert(active_id, price=30_000_000, is_active=True)
    await _insert(archived_id, price=10_000_000, is_active=False,
                  archived_at=datetime.now(timezone.utc))
    try:
        comps, meta = await get_comparables(
            lat=None, lon=None, rooms=2, area=60.0, current_price=30_000_000,
            district=_DISTRICT,
        )
        ids = {c["id"] for c in comps}
        assert ids == {active_id}
    finally:
        await _cleanup(active_id, archived_id)
