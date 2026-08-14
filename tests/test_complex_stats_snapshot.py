"""Регрессия для задачи 2026-08-14 (Г3, docs/data_collection_audit.md):
complex_stats_snapshot.py должен класть ежедневный снимок avg_price_m2/
avg_yield/listings_count в complex_stats_history, по паттерну "имя ИЛИ
resolved_house_id" (дом под зонтиком получает СВОЙ снимок, не зонтика),
идемпотентно при повторном запуске в тот же день.

avg_dom_days/price_drop_share_30d/60d (Фаза L1, docs/location_product_
design.md §7, задача 2026-08-14, миграция 072) — тесты ниже, после
исходных тестов Г3."""
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


@pytest_asyncio.fixture
async def complex_with_listings(db):
    """1 обычный ЖК (2 активных объявления, 500к и 700к ₸/м², yield 8/12%)
    + 1 зонтик с домом (объявление называет зонтика в тексте, но
    resolved_house_id указывает на дом — снимок дома должен отличаться
    от снимка зонтика)."""
    from bot.db.pg import fetchval, execute
    plain_id = await fetchval(
        "INSERT INTO complexes (name) VALUES ('__test_stats_plain__') RETURNING id")
    umbrella_id = await fetchval(
        "INSERT INTO complexes (name, is_umbrella) VALUES ('__test_stats_umbrella__', TRUE) RETURNING id")
    house_id = await fetchval("""
        INSERT INTO complexes (name, parent_complex_id) VALUES ('__test_stats_house__', $1) RETURNING id
    """, umbrella_id)
    await execute("""
        INSERT INTO apartment_listings (id, complex_name, price, area, yield_pct, is_active)
        VALUES
            ('__test_stats_l1__', '__test_stats_plain__', 30000000, 60.0, 8.0, TRUE),
            ('__test_stats_l2__', '__test_stats_plain__', 42000000, 60.0, 12.0, TRUE),
            ('__test_stats_l3__', '__test_stats_umbrella__', 50000000, 100.0, 10.0, TRUE)
    """)
    # l3 "текстом" называет зонтика, но привязан house-resolution'ом к дому.
    await execute(
        "UPDATE apartment_listings SET resolved_house_id = $1 WHERE id = '__test_stats_l3__'", house_id)
    try:
        yield plain_id, umbrella_id, house_id
    finally:
        await execute(
            "DELETE FROM complex_stats_history WHERE complex_id IN ($1, $2, $3)",
            plain_id, umbrella_id, house_id)
        await execute(
            "DELETE FROM apartment_listings WHERE id IN "
            "('__test_stats_l1__', '__test_stats_l2__', '__test_stats_l3__')")
        await execute("DELETE FROM complexes WHERE id IN ($1, $2, $3)", plain_id, umbrella_id, house_id)


@pytest.mark.asyncio
async def test_snapshot_computes_correct_aggregates(complex_with_listings):
    from complex_stats_snapshot import run_snapshot
    from bot.db.pg import fetch

    plain_id, umbrella_id, house_id = complex_with_listings
    await run_snapshot()

    rows = await fetch(
        "SELECT complex_id, avg_price_m2, avg_yield, listings_count FROM complex_stats_history "
        "WHERE complex_id IN ($1, $2, $3) AND date = CURRENT_DATE",
        plain_id, umbrella_id, house_id)
    by_id = {r["complex_id"]: r for r in rows}

    plain = by_id[plain_id]
    assert plain["listings_count"] == 2
    assert round(float(plain["avg_price_m2"])) == round((30_000_000/60 + 42_000_000/60) / 2)
    assert round(float(plain["avg_yield"]), 1) == 10.0  # (8+12)/2

    # Зонтик сам по себе НЕ должен получить снимок — единственное
    # объявление под его именем ушло к дому через resolved_house_id.
    assert umbrella_id not in by_id

    house = by_id[house_id]
    assert house["listings_count"] == 1
    assert round(float(house["avg_price_m2"])) == round(50_000_000 / 100)
    assert round(float(house["avg_yield"]), 1) == 10.0


@pytest.mark.asyncio
async def test_snapshot_idempotent_on_rerun_same_day(complex_with_listings):
    from complex_stats_snapshot import run_snapshot
    from bot.db.pg import fetch

    plain_id, umbrella_id, house_id = complex_with_listings
    await run_snapshot()
    await run_snapshot()  # повторный запуск — не должен плодить дубли

    rows = await fetch(
        "SELECT complex_id FROM complex_stats_history WHERE complex_id = $1 AND date = CURRENT_DATE",
        plain_id)
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_snapshot_updates_values_on_rerun(complex_with_listings):
    from complex_stats_snapshot import run_snapshot
    from bot.db.pg import fetch, execute

    plain_id, umbrella_id, house_id = complex_with_listings
    await run_snapshot()

    # Цена изменилась между прогонами — второй снимок в тот же день должен
    # ОБНОВИТЬ значение (ON CONFLICT DO UPDATE), не оставить старое.
    await execute(
        "UPDATE apartment_listings SET price = 60000000 WHERE id = '__test_stats_l1__'")
    await run_snapshot()

    row = await fetch(
        "SELECT avg_price_m2 FROM complex_stats_history WHERE complex_id = $1 AND date = CURRENT_DATE",
        plain_id)
    expected = round((60_000_000/60 + 42_000_000/60) / 2)
    assert round(float(row[0]["avg_price_m2"])) == expected


# ── Фаза L1: avg_dom_days / price_drop_share_30d / price_drop_share_60d ────

@pytest_asyncio.fixture
async def complex_for_dom_and_drops(db):
    """1 ЖК, 2 активных объявления с известным first_seen — для DOM и
    для доли снижений цены (снижение только у l1, в разных окнах у l2)."""
    from bot.db.pg import fetchval, execute
    cid = await fetchval(
        "INSERT INTO complexes (name) VALUES ('__test_stats_dom__') RETURNING id")
    now = datetime.now(timezone.utc)
    await execute("""
        INSERT INTO apartment_listings (id, complex_name, price, area, is_active, first_seen)
        VALUES
            ('__test_dom_l1__', '__test_stats_dom__', 30000000, 60.0, TRUE, $1),
            ('__test_dom_l2__', '__test_stats_dom__', 42000000, 60.0, TRUE, $2)
    """, now - timedelta(days=10), now - timedelta(days=20))
    try:
        yield cid, now
    finally:
        await execute(
            "DELETE FROM price_history WHERE listing_id IN ('__test_dom_l1__', '__test_dom_l2__')")
        await execute("DELETE FROM complex_stats_history WHERE complex_id = $1", cid)
        await execute(
            "DELETE FROM apartment_listings WHERE id IN ('__test_dom_l1__', '__test_dom_l2__')")
        await execute("DELETE FROM complexes WHERE id = $1", cid)


@pytest.mark.asyncio
async def test_snapshot_avg_dom_days_from_first_seen(complex_for_dom_and_drops):
    from complex_stats_snapshot import run_snapshot
    from bot.db.pg import fetch

    cid, now = complex_for_dom_and_drops
    await run_snapshot()

    row = (await fetch(
        "SELECT avg_dom_days FROM complex_stats_history WHERE complex_id=$1 AND date=CURRENT_DATE", cid))[0]
    # (10 + 20) / 2 = 15 дней, допуск на секунды выполнения теста
    assert float(row["avg_dom_days"]) == pytest.approx(15.0, abs=0.01)


@pytest.mark.asyncio
async def test_snapshot_price_drop_share_zero_when_no_history(complex_for_dom_and_drops):
    """Нет ни одной строки price_history вовсе -> доля 0.0, НЕ NULL —
    "снижений не зафиксировано" валидный измеренный факт, не Unknown."""
    from complex_stats_snapshot import run_snapshot
    from bot.db.pg import fetch

    cid, now = complex_for_dom_and_drops
    await run_snapshot()

    row = (await fetch(
        "SELECT price_drop_share_30d, price_drop_share_60d FROM complex_stats_history "
        "WHERE complex_id=$1 AND date=CURRENT_DATE", cid))[0]
    assert row["price_drop_share_30d"] == pytest.approx(0.0)
    assert row["price_drop_share_60d"] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_snapshot_price_drop_share_counts_drops_within_window(complex_for_dom_and_drops):
    """l1 — снижение 10 дней назад (в обоих окнах). l2 — снижение 45 дней
    назад (в 60d, НЕ в 30d). Доля: 30d=0.5 (только l1), 60d=1.0 (оба)."""
    from complex_stats_snapshot import run_snapshot
    from bot.db.pg import fetch, execute

    cid, now = complex_for_dom_and_drops
    await execute(
        "INSERT INTO price_history (listing_id, old_price, new_price, changed_at) VALUES ($1,$2,$3,$4)",
        "__test_dom_l1__", 32_000_000, 30_000_000, now - timedelta(days=10))
    await execute(
        "INSERT INTO price_history (listing_id, old_price, new_price, changed_at) VALUES ($1,$2,$3,$4)",
        "__test_dom_l2__", 45_000_000, 42_000_000, now - timedelta(days=45))
    await run_snapshot()

    row = (await fetch(
        "SELECT price_drop_share_30d, price_drop_share_60d FROM complex_stats_history "
        "WHERE complex_id=$1 AND date=CURRENT_DATE", cid))[0]
    assert row["price_drop_share_30d"] == pytest.approx(0.5)
    assert row["price_drop_share_60d"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_snapshot_price_increase_not_counted_as_drop(complex_for_dom_and_drops):
    """price_history с new_price > old_price (повышение) не должно
    засчитываться как снижение."""
    from complex_stats_snapshot import run_snapshot
    from bot.db.pg import fetch, execute

    cid, now = complex_for_dom_and_drops
    await execute(
        "INSERT INTO price_history (listing_id, old_price, new_price, changed_at) VALUES ($1,$2,$3,$4)",
        "__test_dom_l1__", 30_000_000, 32_000_000, now - timedelta(days=5))
    await run_snapshot()

    row = (await fetch(
        "SELECT price_drop_share_30d FROM complex_stats_history WHERE complex_id=$1 AND date=CURRENT_DATE", cid))[0]
    assert row["price_drop_share_30d"] == pytest.approx(0.0)
