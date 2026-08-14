"""Регрессия для Часть 2, п.8 (задача 2026-08-14, "скоринг волна 2"):
apartment_listings.effective_score — единая generated column для
score_total+zone_bonus+layer_bonus+price_drop_bonus (с поправкой на
primary_score_total для market_type='primary'), вместо 7 неидентичных
копий формулы в разных SQL-запросах terminal_extras.py."""
import os
import sys

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


@pytest.mark.asyncio
async def test_effective_score_sums_bonuses_for_secondary(db):
    from bot.db.pg import execute, fetch
    await execute("""
        INSERT INTO apartment_listings (id, score_total, zone_bonus, layer_bonus, price_drop_bonus)
        VALUES ('__test_eff_sec__', 70, 5, 3, 10)
    """)
    try:
        row = await fetch("SELECT effective_score FROM apartment_listings WHERE id=$1", "__test_eff_sec__")
        assert row[0]["effective_score"] == 88  # 70+5+3+10
    finally:
        await execute("DELETE FROM apartment_listings WHERE id='__test_eff_sec__'")


@pytest.mark.asyncio
async def test_effective_score_handles_null_bonuses(db):
    from bot.db.pg import execute, fetch
    await execute("""
        INSERT INTO apartment_listings (id, score_total) VALUES ('__test_eff_null__', 50)
    """)
    try:
        row = await fetch("SELECT effective_score FROM apartment_listings WHERE id=$1", "__test_eff_null__")
        assert row[0]["effective_score"] == 50  # NULL-бонусы -> 0, не роняют выражение
    finally:
        await execute("DELETE FROM apartment_listings WHERE id='__test_eff_null__'")


@pytest.mark.asyncio
async def test_effective_score_uses_primary_score_total_for_primary_listings(db):
    """Живой баг, найденный при унификации: 43 активных primary-объявления
    на момент миграции имели score_total IS DISTINCT FROM primary_score_
    total — 6 из 7 старых копий формулы использовали голый score_total,
    только /admin/api/map-points ("eff_score") уже учитывал primary_
    score_total. effective_score — единая колонка, всегда правильная."""
    from bot.db.pg import execute, fetch
    await execute("""
        INSERT INTO apartment_listings (id, market_type, score_total, primary_score_total, zone_bonus)
        VALUES ('__test_eff_primary__', 'primary', 20, 85, 2)
    """)
    try:
        row = await fetch("SELECT effective_score FROM apartment_listings WHERE id=$1", "__test_eff_primary__")
        assert row[0]["effective_score"] == 87  # 85 (primary_score_total) + 2, НЕ 20+2
    finally:
        await execute("DELETE FROM apartment_listings WHERE id='__test_eff_primary__'")


@pytest.mark.asyncio
async def test_effective_score_recomputes_on_update(db):
    from bot.db.pg import execute, fetch
    await execute("""
        INSERT INTO apartment_listings (id, score_total, zone_bonus) VALUES ('__test_eff_upd__', 40, 0)
    """)
    try:
        await execute("UPDATE apartment_listings SET zone_bonus = 15 WHERE id='__test_eff_upd__'")
        row = await fetch("SELECT effective_score FROM apartment_listings WHERE id=$1", "__test_eff_upd__")
        assert row[0]["effective_score"] == 55  # пересчитано автоматически (GENERATED ALWAYS)
    finally:
        await execute("DELETE FROM apartment_listings WHERE id='__test_eff_upd__'")


@pytest.mark.asyncio
async def test_effective_score_column_is_not_directly_writable(db):
    from bot.db.pg import execute
    with pytest.raises(Exception):
        await execute(
            "INSERT INTO apartment_listings (id, effective_score) VALUES ('__test_eff_write__', 99)")


@pytest.mark.asyncio
async def test_effective_score_matches_score_total_across_live_table(db):
    """Фаза A.5, п.6 вердикт-стратегии (задача 2026-08-14, docs/
    verdict_strategy.md) — после того как Фаза A поменяла ФОРМУЛУ
    score_total (убран price->quality proxy, W_RISK=0, см. Часть 6
    п.4/п.6 scoring_roadmap.md), нужно было проверить, не разъехалась ли
    effective_score. Ответ структурный: GENERATED ALWAYS ... STORED
    ссылается на score_total НАПРЯМУЮ (не дублирует формулу) — расхождение
    физически невозможно независимо от того, как считается сам
    score_total на стороне Python (bot/core/deal_score.py). Живая
    проверка на ВСЕЙ таблице (не синтетике) — гейт-число для отчёта."""
    from bot.db.pg import fetchrow
    row = await fetchrow("""
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (
                WHERE market_type != 'primary' AND effective_score IS DISTINCT FROM
                    COALESCE(score_total,0) + COALESCE(zone_bonus,0) + COALESCE(layer_bonus,0) + COALESCE(price_drop_bonus,0)
            ) AS secondary_mismatch,
            COUNT(*) FILTER (
                WHERE market_type = 'primary' AND primary_score_total IS NOT NULL AND effective_score IS DISTINCT FROM
                    primary_score_total + COALESCE(zone_bonus,0) + COALESCE(layer_bonus,0) + COALESCE(price_drop_bonus,0)
            ) AS primary_mismatch
        FROM apartment_listings
    """)
    assert row["secondary_mismatch"] == 0
    assert row["primary_mismatch"] == 0
