"""Регрессия для Фазы L1 продуктового трека «Локация» (docs/location_
product_design.md §7, задача 2026-08-14), коммит 4 —
hex_market_stats_snapshot.py: плотность предложения по гексагону
(bot/core/hexgrid.hex_id(), группировка в Python, не GROUP BY в SQL).
Реальная БД (тот же паттерн, что tests/test_complex_stats_snapshot.py).

resolved_house_id НЕ участвует здесь намеренно — агрегация по
координате объявления, не по имени ЖК (см. докстринг writer-скрипта).

Координаты тестовых объявлений — ЗАВЕДОМО далеко от Астаны (около
экватора, lat~1/lon~1), НЕ в _LAT0/_LON0 hexgrid.py (это буквально
центр Астаны — реальные данные там очень плотные, тест на них ловил бы
чужие живые объявления в тот же гексагон). run_snapshot(listing_ids=...)
— тот же паттерн скоупинга, что у deal_score_snapshot.py, чтобы прогон
теста не пересчитывал всю прод-таблицу (~30к+ объявлений) и не
перезаписывал реальные строки hex_market_stats за сегодня апсертом."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

# Далеко в стороне от Астаны (ASTANA_BBOX в bot/core/geo.py:
# lat 50.70-51.55, lon 70.80-72.10) — гарантированно свой, ничейный
# гексагон, никаких реальных объявлений там не бывает.
_FAR_LAT, _FAR_LON = 1.1280, 1.4300
_FAR2_LAT, _FAR2_LON = 5.2000, 5.6000

_CLUSTER_IDS = ["__test_hms_l1__", "__test_hms_l2__", "__test_hms_l3__"]
_FAR_ID = "__test_hms_far__"
_ALL_IDS = _CLUSTER_IDS + [_FAR_ID]


@pytest_asyncio.fixture
async def db():
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    yield
    await close_pool()


@pytest.fixture(autouse=True)
def _fixed_hex_edge_m():
    """run_snapshot() читает HEX_EDGE_M через bot.db.settings::get_int(),
    который берёт значение из module-level _cache — populated ТОЛЬКО
    после явного app_settings.load() где-то в процессе (settings.py
    докстринг). В полном прогоне suite это уже произошло к моменту этого
    файла (прод-БД реально хранит HEX_EDGE_M=100, не дефолт 50) — без
    фиксации тест сверял бы hex_id, посчитанный с edge=50, против строки,
    которую run_snapshot() реально записал с edge=100 (падение только в
    полном прогоне, не в изоляции — живой пример "тест ловит state
    leakage через module-level кеш между тестами"). Правим ТОЛЬКО
    in-process кеш (не пишем в реальную app_settings в БД) и
    восстанавливаем как было."""
    from bot.db import settings as app_settings
    had_key = "HEX_EDGE_M" in app_settings._cache
    prev = app_settings._cache.get("HEX_EDGE_M")
    app_settings._cache["HEX_EDGE_M"] = "50"
    yield
    if had_key:
        app_settings._cache["HEX_EDGE_M"] = prev
    else:
        app_settings._cache.pop("HEX_EDGE_M", None)


@pytest_asyncio.fixture
async def two_hex_clusters(db):
    """3 объявления кучкуются в одном гексагоне (почти одна точка,
    далеко от Астаны), 1 — далеко в стороне от них (отдельный гексагон,
    ещё дальше)."""
    from bot.db.pg import execute
    await execute("""
        INSERT INTO apartment_listings (id, price, area, lat, lon, is_active, is_duplicate)
        VALUES
            ($1, 30000000, 60.0, $5, $6, TRUE, FALSE),
            ($2, 42000000, 60.0, $5, $6, TRUE, FALSE),
            ($3, 36000000, NULL,  $5, $6, TRUE, FALSE),
            ($4, 50000000, 100.0, $7, $8, TRUE, FALSE)
    """, *_ALL_IDS, _FAR_LAT, _FAR_LON, _FAR2_LAT, _FAR2_LON)
    try:
        yield _ALL_IDS
    finally:
        cluster_hex, far_hex = _hex_ids()
        await execute("DELETE FROM hex_market_stats WHERE hex_id = ANY($1::text[]) AND date = CURRENT_DATE",
                       [cluster_hex, far_hex])
        await execute("DELETE FROM apartment_listings WHERE id = ANY($1::text[])", _ALL_IDS)


def _hex_ids():
    from bot.core.hexgrid import hex_id as compute_hex_id
    return compute_hex_id(_FAR_LAT, _FAR_LON, 50.0), compute_hex_id(_FAR2_LAT, _FAR2_LON, 50.0)


@pytest.mark.asyncio
async def test_snapshot_groups_by_hex_not_by_listing(two_hex_clusters):
    from hex_market_stats_snapshot import run_snapshot
    from bot.db.pg import fetch

    ids = two_hex_clusters
    cluster_hex, far_hex = _hex_ids()
    assert cluster_hex != far_hex

    n = await run_snapshot(listing_ids=ids)
    assert n == 2  # ровно 2 гексагона в скоупе теста

    rows = await fetch(
        "SELECT hex_id, listings_count, avg_price_m2 FROM hex_market_stats "
        "WHERE hex_id = ANY($1::text[]) AND date = CURRENT_DATE",
        [cluster_hex, far_hex])
    by_hex = {r["hex_id"]: r for r in rows}

    assert by_hex[cluster_hex]["listings_count"] == 3  # 3 сгрудившихся объявления
    # avg_price_m2 считается только по l1/l2 (у l3 area IS NULL -> исключён):
    expected = (30_000_000 / 60.0 + 42_000_000 / 60.0) / 2
    assert float(by_hex[cluster_hex]["avg_price_m2"]) == pytest.approx(expected, rel=1e-6)
    assert by_hex[far_hex]["listings_count"] == 1


@pytest.mark.asyncio
async def test_snapshot_idempotent_on_rerun_same_day(two_hex_clusters):
    from hex_market_stats_snapshot import run_snapshot
    from bot.db.pg import fetch

    ids = two_hex_clusters
    cluster_hex, _ = _hex_ids()

    await run_snapshot(listing_ids=ids)
    await run_snapshot(listing_ids=ids)  # повторный прогон — не дублирует строку

    rows = await fetch(
        "SELECT hex_id FROM hex_market_stats WHERE hex_id = $1 AND date = CURRENT_DATE", cluster_hex)
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_snapshot_excludes_inactive_and_duplicate_listings(db):
    from bot.db.pg import execute, fetch
    from hex_market_stats_snapshot import run_snapshot
    from bot.core.hexgrid import hex_id as compute_hex_id

    active_id, archived_id, dup_id = "__test_hms_active__", "__test_hms_archived__", "__test_hms_dup__"
    lat, lon = 2.5000, 2.7000  # свой отдельный ничейный гексагон
    hid = compute_hex_id(lat, lon, 50.0)
    all_ids = [active_id, archived_id, dup_id]
    await execute("""
        INSERT INTO apartment_listings (id, price, area, lat, lon, is_active, is_duplicate)
        VALUES
            ($1, 30000000, 60.0, $4, $5, TRUE, FALSE),
            ($2, 30000000, 60.0, $4, $5, FALSE, FALSE),
            ($3, 30000000, 60.0, $4, $5, TRUE, TRUE)
    """, *all_ids, lat, lon)
    try:
        # Скоуп включает archived_id/dup_id намеренно — проверяем, что
        # is_active/is_duplicate фильтр в SQL их всё равно исключит, не
        # то что listing_ids сам по себе достаточен.
        n = await run_snapshot(listing_ids=all_ids)
        assert n == 1
        row = await fetch(
            "SELECT listings_count FROM hex_market_stats WHERE hex_id=$1 AND date=CURRENT_DATE", hid)
        assert len(row) == 1
        assert row[0]["listings_count"] == 1  # только active_id
    finally:
        await execute("DELETE FROM hex_market_stats WHERE hex_id=$1 AND date=CURRENT_DATE", hid)
        await execute("DELETE FROM apartment_listings WHERE id = ANY($1::text[])", all_ids)


@pytest.mark.asyncio
async def test_snapshot_stores_edge_m_from_app_settings(two_hex_clusters):
    from hex_market_stats_snapshot import run_snapshot
    from bot.db.pg import fetch

    ids = two_hex_clusters
    cluster_hex, _ = _hex_ids()

    await run_snapshot(listing_ids=ids)
    row = (await fetch(
        "SELECT edge_m FROM hex_market_stats WHERE hex_id=$1 AND date=CURRENT_DATE", cluster_hex))[0]
    assert row["edge_m"] == pytest.approx(50.0)
