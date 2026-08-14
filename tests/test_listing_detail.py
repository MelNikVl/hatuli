"""Регрессия для Фазы B, п.5 вердикт-стратегии (docs/verdict_strategy.md,
задача 2026-08-14): bot/core/listing_detail — сборка ответа для модалки
объявления и истории цены, вынесенная из terminal_extras.py ("роут не
знает SQL"). Реальная БД (тот же паттерн, что tests/test_effective_score.py) —
build_listing_detail()/build_price_history() делают живые SQL-запросы,
не чистые функции. Поведение НЕ менялось при переносе — тесты защищают
от регресса, не чинят новый баг."""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

_DISTRICT = "__Тестовый Детали р-н__"


@pytest_asyncio.fixture
async def db():
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    yield
    await close_pool()


async def _insert_listing(id_, **overrides):
    from bot.db.pg import execute
    defaults = dict(
        price=30_000_000, area=60.0, rooms=2, address="ул. Тестовая, 1",
        district=_DISTRICT, complex_name=None, floor=5, floors_total=12,
        lat=None, lon=None, is_active=True, is_duplicate=False,
        first_seen=datetime.now(timezone.utc) - timedelta(days=10),
    )
    defaults.update(overrides)
    cols = ["id"] + list(defaults.keys())
    placeholders = ", ".join(f"${i+1}" for i in range(len(cols)))
    await execute(
        f"INSERT INTO apartment_listings ({', '.join(cols)}) VALUES ({placeholders})",
        id_, *defaults.values(),
    )


async def _cleanup(*ids):
    from bot.db.pg import execute
    await execute("DELETE FROM apartment_listings WHERE id = ANY($1::text[])", list(ids))
    await execute("DELETE FROM price_history WHERE listing_id = ANY($1::text[])", list(ids))


@pytest.mark.asyncio
async def test_not_found_raises(db):
    from bot.core.listing_detail import build_listing_detail, ListingNotFound
    with pytest.raises(ListingNotFound):
        await build_listing_detail("__test_ld_missing__", tier="admin")


@pytest.mark.asyncio
async def test_public_tier_raises_restricted_even_when_found(db):
    from bot.core.listing_detail import build_listing_detail, ListingRestricted
    lid = "__test_ld_public__"
    await _insert_listing(lid)
    try:
        with pytest.raises(ListingRestricted):
            await build_listing_detail(lid, tier="public")
    finally:
        await _cleanup(lid)


@pytest.mark.asyncio
async def test_full_listing_shape_and_field_mapping(db):
    from bot.core.listing_detail import build_listing_detail
    lid = "__test_ld_full__"
    await _insert_listing(
        lid, ceiling_height=2.75, kitchen_area=8.5, trust_score=91,
        hex_details='{"deal": "good", "confidence": 70}',
        score_yield=10, score_price_market=20,
    )
    try:
        payload = await build_listing_detail(lid, tier="admin")
        assert payload["id"] == lid
        assert payload["price"] == 30_000_000
        assert payload["tier"] == "admin"
        assert payload["ceiling_height"] == pytest.approx(2.75)
        assert payload["kitchen_area"] == pytest.approx(8.5)
        assert payload["deal_score"] == {"deal": "good", "confidence": 70}
        assert payload["score_breakdown"]["yield"] == 10
        assert payload["score_breakdown"]["price_market"] == 20
        assert isinstance(payload["bargain"], dict)
        assert isinstance(payload["negotiation_points"], list)
        assert isinstance(payload["seller_questions"], list)
        assert payload["similar"] == []  # нет lat/lon -> compute_similar_listings по своим правилам, но nearby точно пуст
        assert payload["nearby"] == []   # lat/lon не заданы -> лента "рядом" не считается
    finally:
        await _cleanup(lid)


@pytest.mark.asyncio
async def test_missing_ceiling_and_kitchen_area_stay_none_not_zero(db):
    """Unknown != average (docs/verdict_strategy.md §3.1) — на уровне
    самого поля ответа, не только скоринга."""
    from bot.core.listing_detail import build_listing_detail
    lid = "__test_ld_unknown_fields__"
    await _insert_listing(lid)
    try:
        payload = await build_listing_detail(lid, tier="admin")
        assert payload["ceiling_height"] is None
        assert payload["kitchen_area"] is None
        assert payload["deal_score"] is None
    finally:
        await _cleanup(lid)


@pytest.mark.asyncio
async def test_price_history_empty_starts_from_first_seen(db):
    from bot.core.listing_detail import build_price_history
    lid = "__test_ph_empty__"
    await _insert_listing(lid, price=25_000_000)
    try:
        result = await build_price_history(lid)
        assert result["changes"] == 0
        assert result["current"] == 25_000_000
        assert len(result["points"]) == 1
        assert result["points"][0]["price"] == 25_000_000
    finally:
        await _cleanup(lid)


@pytest.mark.asyncio
async def test_price_history_with_changes_orders_ascending(db):
    from bot.db.pg import execute
    from bot.core.listing_detail import build_price_history
    lid = "__test_ph_changes__"
    await _insert_listing(lid, price=27_000_000)
    t0 = datetime.now(timezone.utc) - timedelta(days=5)
    t1 = datetime.now(timezone.utc) - timedelta(days=2)
    try:
        await execute(
            "INSERT INTO price_history (listing_id, old_price, new_price, changed_at) VALUES ($1,$2,$3,$4)",
            lid, 30_000_000, 28_000_000, t0)
        await execute(
            "INSERT INTO price_history (listing_id, old_price, new_price, changed_at) VALUES ($1,$2,$3,$4)",
            lid, 28_000_000, 27_000_000, t1)
        result = await build_price_history(lid)
        assert result["changes"] == 2
        assert result["current"] == 27_000_000
        prices = [p["price"] for p in result["points"]]
        assert prices == [30_000_000, 28_000_000, 27_000_000]
    finally:
        await _cleanup(lid)
