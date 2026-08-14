"""Регрессия для задачи 2026-08-14 ("as_of для score_total, минимальный
план" — по итогам аудита временной логики перед Фазой B, см.
docs/verdict_strategy.md): _activity_filter() централизована в
bot/core/hedonic_constants.py, используется и bargain.py, и deal_score.py
(compute_deal_scores()/apply_deal_scores()). Реальная БД (тот же паттерн,
что tests/test_effective_score.py) — apply_deal_scores() делает живые
SQL-запросы, не чистая функция."""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")


@pytest_asyncio.fixture
async def db():
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    yield
    await close_pool()


async def _insert(id_, price=30_000_000, area=60.0, rooms=2, lat=51.10, lon=71.40,
                   is_active=True, archived_at=None, first_seen=None, market_type="secondary"):
    from bot.db.pg import execute
    await execute(
        """
        INSERT INTO apartment_listings
            (id, price, area, rooms, lat, lon, is_active, archived_at, first_seen, market_type)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8, COALESCE($9, now()), $10)
        """,
        id_, price, area, rooms, lat, lon, is_active, archived_at, first_seen, market_type,
    )


async def _cleanup(*ids):
    from bot.db.pg import execute
    await execute("DELETE FROM apartment_listings WHERE id = ANY($1::text[])", list(ids))


# ── _activity_filter централизована — одна и та же функция в обоих модулях ──

def test_activity_filter_same_function_in_both_modules():
    from bot.core.bargain import _activity_filter as bargain_af
    from bot.core.deal_score import _activity_filter as deal_score_af
    from bot.core.hedonic_constants import _activity_filter as hc_af
    assert bargain_af is hc_af
    assert deal_score_af is hc_af


def test_activity_filter_none_gives_plain_is_active():
    from bot.core.hedonic_constants import _activity_filter
    sql, params = _activity_filter(None, 1)
    assert "is_active IS NOT FALSE" in sql
    assert params == []


def test_activity_filter_with_date_gives_point_in_time_reconstruction():
    from bot.core.hedonic_constants import _activity_filter
    t0 = datetime(2026, 7, 25, tzinfo=timezone.utc)
    sql, params = _activity_filter(t0, 1)
    assert "first_seen <= $1" in sql
    assert "archived_at IS NULL OR" in sql and "archived_at > $1" in sql
    assert params == [t0]


# ── compute_deal_scores(): as_of — только тег в выводе, не фильтрация ──

def test_compute_deal_scores_tags_as_of_in_output():
    from bot.core.deal_score import compute_deal_scores
    t0 = datetime(2026, 7, 25, tzinfo=timezone.utc)
    listing = {
        "id": "A", "lat": 51.10, "lon": 71.40, "price": 30_000_000, "area": 60.0,
        "rooms": 2, "floor": 5, "floors_total": 12, "year_built": 2020,
        "complex_name": "ЖК Тест", "is_owner": True, "district": "Есильский р-н",
        "yield_pct": 8.0, "same_complex_cnt": 1, "ceiling_height": None,
        "resolved_house_id": None, "finish_level": None,
    }
    result = compute_deal_scores([listing], {}, edge_m=100.0, as_of=t0)
    assert result["A"]["as_of"] == t0.isoformat()


def test_compute_deal_scores_as_of_none_by_default():
    from bot.core.deal_score import compute_deal_scores
    listing = {
        "id": "B", "lat": 51.10, "lon": 71.40, "price": 30_000_000, "area": 60.0,
        "rooms": 2, "floor": 5, "floors_total": 12, "year_built": 2020,
        "complex_name": "ЖК Тест", "is_owner": True, "district": "Есильский р-н",
        "yield_pct": 8.0, "same_complex_cnt": 1, "ceiling_height": None,
        "resolved_house_id": None, "finish_level": None,
    }
    result = compute_deal_scores([listing], {}, edge_m=100.0)
    assert result["B"]["as_of"] is None


# ── apply_deal_scores(as_of=t0) — реальная точечная реконструкция на БД ──

@pytest.mark.asyncio
async def test_as_of_excludes_listing_created_after_t0(db):
    from bot.core.deal_score import apply_deal_scores
    now = datetime.now(timezone.utc)
    t0 = now - timedelta(days=20)
    future_id = "__test_asof_future__"
    await _insert(future_id, first_seen=now - timedelta(days=5))  # появилось ПОСЛЕ t0
    try:
        result = await apply_deal_scores(as_of=t0)
        assert future_id not in result
    finally:
        await _cleanup(future_id)


@pytest.mark.asyncio
async def test_as_of_excludes_listing_archived_before_t0(db):
    from bot.core.deal_score import apply_deal_scores
    now = datetime.now(timezone.utc)
    t0 = now - timedelta(days=20)
    old_id = "__test_asof_archived_before__"
    await _insert(old_id, first_seen=now - timedelta(days=60), is_active=False,
                  archived_at=now - timedelta(days=30))  # архивировано ДО t0
    try:
        result = await apply_deal_scores(as_of=t0)
        assert old_id not in result
    finally:
        await _cleanup(old_id)


@pytest.mark.asyncio
async def test_as_of_includes_listing_active_at_t0_even_if_archived_now(db):
    # Ключевой сценарий: объявление СЕЙЧАС в архиве (is_active=FALSE), но
    # архивировано ПОСЛЕ t0 — значит на t0 оно было активно. is_active
    # текущий != "было активно на t0", это и есть суть задачи.
    from bot.core.deal_score import apply_deal_scores
    now = datetime.now(timezone.utc)
    t0 = now - timedelta(days=20)
    active_id = "__test_asof_active_at_t0__"
    await _insert(active_id, first_seen=now - timedelta(days=40), is_active=False,
                  archived_at=now - timedelta(days=5))
    try:
        result = await apply_deal_scores(as_of=t0)
        assert active_id in result
        assert result[active_id]["as_of"] == t0.isoformat()
    finally:
        await _cleanup(active_id)


@pytest.mark.asyncio
async def test_as_of_backtest_does_not_write_to_db(db):
    from bot.core.deal_score import apply_deal_scores
    from bot.db.pg import fetchrow
    now = datetime.now(timezone.utc)
    t0 = now - timedelta(days=20)
    lid = "__test_asof_no_write__"
    await _insert(lid, first_seen=now - timedelta(days=40))
    try:
        row_before = await fetchrow("SELECT score_total, hex_details FROM apartment_listings WHERE id=$1", lid)
        assert row_before["score_total"] is None
        result = await apply_deal_scores(as_of=t0)
        assert lid in result
        assert result[lid]["deal"] is not None  # посчитано в Python
        row_after = await fetchrow("SELECT score_total, hex_details FROM apartment_listings WHERE id=$1", lid)
        assert row_after["score_total"] is None  # НЕ записано в БД
        assert row_after["hex_details"] is None
    finally:
        await _cleanup(lid)


@pytest.mark.asyncio
async def test_as_of_returns_dict_not_int(db):
    from bot.core.deal_score import apply_deal_scores
    now = datetime.now(timezone.utc)
    t0 = now - timedelta(days=20)
    lid = "__test_asof_return_type__"
    await _insert(lid, first_seen=now - timedelta(days=40))
    try:
        result = await apply_deal_scores(as_of=t0)
        assert isinstance(result, dict)
    finally:
        await _cleanup(lid)


# ── прод-путь (as_of=None) — поведение не изменилось ──

@pytest.mark.asyncio
async def test_prod_path_without_as_of_still_writes_and_returns_int(db):
    from bot.core.deal_score import apply_deal_scores
    from bot.db.pg import fetchrow
    now = datetime.now(timezone.utc)
    lid = "__test_asof_prod_path__"
    await _insert(lid, first_seen=now - timedelta(days=5), is_active=True)
    try:
        n = await apply_deal_scores()  # as_of не передан — как раньше
        assert isinstance(n, int)
        row = await fetchrow("SELECT score_total, hex_details FROM apartment_listings WHERE id=$1", lid)
        assert row["score_total"] is not None
        assert row["hex_details"] is not None
    finally:
        await _cleanup(lid)
