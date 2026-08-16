"""Регрессия для задачи 2026-08-16 (мульти-источниковый сбор отзывов) —
миграция 081 (reviews_raw) + чистые функции reviews_pipeline.py
(text_hash/dedupe_reviews). Реальная БД для схемы (тот же паттерн, что
tests/test_complex_walkability_schema.py), чистые функции — без БД."""
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


# ── чистые функции reviews_pipeline ──────────────────────────────────────

def test_text_hash_normalizes_case_and_whitespace():
    from reviews_pipeline import text_hash
    assert text_hash("  Хороший ЖК,\nНО  шумно ") == text_hash("хороший жк, но шумно")
    assert text_hash("текст а") != text_hash("текст б")


def test_dedupe_cross_post_keeps_first_source_marks_also_on():
    """Один отзыв, скопированный в 2gis и google_maps (тот же автор/дата/
    текст) — ОДНА строка у первого по приоритету источника, второй
    источник виден в raw->>'also_on'."""
    from reviews_pipeline import dedupe_reviews, text_hash
    t = "Отличный двор, но парковки мало"
    a = dedupe_reviews([
        {"source": "2gis", "author": "Айгерим", "review_date": "2026-08-01",
         "text": t, "text_hash": text_hash(t)},
        {"source": "google_maps", "author": "айгерим ", "review_date": "2026-08-01",
         "text": t + " ", "text_hash": text_hash(t + " ")},
    ])
    assert len(a) == 1
    assert a[0]["source"] == "2gis"
    assert a[0]["raw"]["also_on"] == ["google_maps"]


def test_dedupe_keeps_different_authors_or_dates():
    from reviews_pipeline import dedupe_reviews, text_hash
    t = "одинаковый текст"
    a = dedupe_reviews([
        {"source": "2gis", "author": "А", "review_date": "2026-08-01",
         "text": t, "text_hash": text_hash(t)},
        {"source": "2gis", "author": "Б", "review_date": "2026-08-01",
         "text": t, "text_hash": text_hash(t)},
        {"source": "2gis", "author": "А", "review_date": "2026-07-01",
         "text": t, "text_hash": text_hash(t)},
    ])
    assert len(a) == 3


# ── схема reviews_raw ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reviews_raw_insert_roundtrip_and_uniqueness(db):
    from bot.db.pg import execute, fetchrow, fetchval
    cid = await fetchval("INSERT INTO complexes (name) VALUES ('__test_rr__') RETURNING id")
    try:
        await execute("""
            INSERT INTO reviews_raw (complex_id, source, review_text, text_hash, author)
            VALUES ($1, '2gis', 'двор супер', 'hash1', 'Автор')
        """, cid)
        row = await fetchrow("SELECT * FROM reviews_raw WHERE complex_id=$1", cid)
        assert row["sentiment"] is None and row["classified_at"] is None
        assert row["fetched_at"] is not None
        # повтор той же тройки (complex_id, source, text_hash) — конфликт
        with pytest.raises(Exception):
            await execute("""
                INSERT INTO reviews_raw (complex_id, source, review_text, text_hash)
                VALUES ($1, '2gis', 'двор супер', 'hash1')
            """, cid)
        # другой источник с тем же хэшем — НЕ конфликт (кросс-дедуп —
        # уровень оркестратора, не схемы)
        await execute("""
            INSERT INTO reviews_raw (complex_id, source, review_text, text_hash)
            VALUES ($1, 'google_maps', 'двор супер', 'hash1')
        """, cid)
    finally:
        await execute("DELETE FROM complexes WHERE id=$1", cid)  # cascade
        rows = await fetchval("SELECT count(*) FROM reviews_raw WHERE complex_id=$1", cid)
        assert rows == 0


@pytest.mark.asyncio
async def test_reviews_raw_source_check_constraint(db):
    from bot.db.pg import execute, fetchval
    cid = await fetchval("INSERT INTO complexes (name) VALUES ('__test_rr_src__') RETURNING id")
    try:
        with pytest.raises(Exception):
            await execute("""
                INSERT INTO reviews_raw (complex_id, source, review_text, text_hash)
                VALUES ($1, 'telegram', 'текст', 'h')
            """, cid)
    finally:
        await execute("DELETE FROM complexes WHERE id=$1", cid)
