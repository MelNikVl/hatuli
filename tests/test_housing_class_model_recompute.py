"""Регрессия для Фазы B, п.3 вердикт-стратегии (docs/verdict_strategy.md,
задача 2026-08-14): housing_class_model_recompute.py — обучение +
применение класс-модели на реальной БД. Полный прогон дешёвый (~2097
complexes, ~3с, не апартаменты) — отдельного listing_ids-скоупинга не
требуется, в отличие от apartment_listings-масштабных скриптов."""
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


async def _insert_complex(name, housing_class=None, avg_price_m2=None, year_built=None):
    from bot.db.pg import fetchval
    return await fetchval(
        """
        INSERT INTO complexes (name, housing_class, avg_price_m2, year_built, is_garbage)
        VALUES ($1, $2, $3, $4, FALSE) RETURNING id
        """,
        name, housing_class, avg_price_m2, year_built,
    )


async def _cleanup(*ids):
    from bot.db.pg import execute
    await execute("DELETE FROM complexes WHERE id = ANY($1::int[])", list(ids))


@pytest.mark.asyncio
async def test_manual_label_preserved_not_overwritten_by_prediction(db):
    from housing_class_model_recompute import run_recompute
    from bot.db.pg import fetchrow
    cid = await _insert_complex("__test_hcm_manual__", housing_class="элит",
                                 avg_price_m2=900_000, year_built=2023)
    try:
        await run_recompute()
        row = await fetchrow(
            "SELECT predicted_housing_class, predicted_housing_class_probability, "
            "predicted_housing_class_source FROM complexes WHERE id=$1", cid)
        assert row["predicted_housing_class_source"] == "manual"
        assert row["predicted_housing_class"] is None
        assert row["predicted_housing_class_probability"] is None
    finally:
        await _cleanup(cid)


@pytest.mark.asyncio
async def test_unlabeled_with_known_features_gets_prediction(db):
    from housing_class_model_recompute import run_recompute
    from bot.db.pg import fetchrow
    cid = await _insert_complex("__test_hcm_predict__", housing_class=None,
                                 avg_price_m2=850_000, year_built=2024)
    try:
        await run_recompute()
        row = await fetchrow(
            "SELECT predicted_housing_class, predicted_housing_class_probability, "
            "predicted_housing_class_source, predicted_housing_class_computed_at "
            "FROM complexes WHERE id=$1", cid)
        assert row["predicted_housing_class_source"] == "predicted"
        assert row["predicted_housing_class"] in ("элит", "бизнес", "комфорт", "эконом")
        assert 0.0 <= row["predicted_housing_class_probability"] <= 1.0
        assert row["predicted_housing_class_computed_at"] is not None
    finally:
        await _cleanup(cid)


@pytest.mark.asyncio
async def test_missing_features_gets_null_source_not_guessed(db):
    from housing_class_model_recompute import run_recompute
    from bot.db.pg import fetchrow
    cid = await _insert_complex("__test_hcm_unknown__", housing_class=None,
                                 avg_price_m2=None, year_built=None)
    try:
        await run_recompute()
        row = await fetchrow(
            "SELECT predicted_housing_class, predicted_housing_class_source "
            "FROM complexes WHERE id=$1", cid)
        assert row["predicted_housing_class_source"] is None
        assert row["predicted_housing_class"] is None
    finally:
        await _cleanup(cid)


@pytest.mark.asyncio
async def test_unmappable_manual_label_treated_as_no_manual_label(db):
    # "премиум" не входит в 4-тиерную таксономию -> normalize_label даёт
    # None -> НЕ считается ручной меткой для целей source='manual', сам
    # complex получает предсказание, как если бы housing_class был пуст
    # (см. докстринг recompute-скрипта: manual_cls = normalize_label(...)).
    from housing_class_model_recompute import run_recompute
    from bot.db.pg import fetchrow
    cid = await _insert_complex("__test_hcm_premium__", housing_class="премиум",
                                 avg_price_m2=950_000, year_built=2024)
    try:
        await run_recompute()
        row = await fetchrow(
            "SELECT predicted_housing_class_source FROM complexes WHERE id=$1", cid)
        assert row["predicted_housing_class_source"] == "predicted"
    finally:
        await _cleanup(cid)


@pytest.mark.asyncio
async def test_recompute_returns_summary_counts(db):
    from housing_class_model_recompute import run_recompute
    summary = await run_recompute()
    assert summary["total"] == summary["manual"] + summary["predicted"] + summary["unknown"]
    assert summary["holdout"]["accuracy"] is not None
