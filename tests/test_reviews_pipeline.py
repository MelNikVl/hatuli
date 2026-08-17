"""Регрессия для задачи 2026-08-16 (мульти-источниковый сбор отзывов) —
миграция 081 (reviews_raw) + чистые функции reviews_pipeline.py
(text_hash/dedupe_reviews). Реальная БД для схемы (тот же паттерн, что
tests/test_complex_walkability_schema.py), чистые функции — без БД."""
import json
import os
import sys
from datetime import date

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


# ── Задача 2026-08-17: ложный sentinel — 4 исхода источника ──────────────
# (успешный ноль / временная ошибка / постоянная ошибка / успешные данные)

@pytest_asyncio.fixture
async def cx(db):
    """Тестовый ЖК — для collect_one_complex нужен настоящий complex_id
    (FK reviews_raw.complex_id)."""
    from bot.db.pg import execute, fetchval
    cid = await fetchval("INSERT INTO complexes (name) VALUES ('__test_rp_sentinel__') RETURNING id")
    yield {"id": cid, "name": "__test_rp_sentinel__", "developer_id": None}
    await execute("DELETE FROM complexes WHERE id=$1", cid)  # cascade delete reviews_raw


def _patch_sources(monkeypatch, *coros):
    import reviews_pipeline
    monkeypatch.setattr(reviews_pipeline, "_SOURCES", list(coros))


@pytest.mark.asyncio
async def test_successful_zero_reviews_writes_sentinel(cx, monkeypatch):
    """Исход 1: успешный ответ с нулём отзывов -> sentinel пишется
    (source — реальное значение из CHECK-ограничения миграции 081:
    '2gis'/'google_maps'/'yandex', имя функции определяет source-колонку)."""
    from reviews_pipeline import collect_one_complex
    from bot.db.pg import fetchrow

    async def collect_google_maps(_cx):
        return []

    _patch_sources(monkeypatch, collect_google_maps)
    result = await collect_one_complex(cx, None)
    assert result["empty_sources"] == ["google_maps"]
    assert result["failed_sources"] == []

    row = await fetchrow(
        "SELECT review_text, raw, classified_at FROM reviews_raw WHERE complex_id=$1 AND source='google_maps'",
        cx["id"])
    assert row is not None
    assert row["review_text"] == ""
    assert json.loads(row["raw"])["empty"] is True  # jsonb приходит text'ом без кастомного codec
    assert row["classified_at"] is not None


@pytest.mark.asyncio
async def test_transient_error_does_not_write_sentinel_preserves_old_data(cx, monkeypatch):
    """Исход 2: временная ошибка (timeout/5xx/429) -> НЕ sentinel, старые
    данные того же источника не трогаются."""
    import importlib
    twogis = importlib.import_module("2gis_reviews_collect")
    from reviews_pipeline import collect_one_complex, text_hash
    from bot.db.pg import execute, fetchval, fetch

    # Старая (ранее успешно собранная) строка для source='2gis' — должна
    # пережить прогон, где 2gis сейчас падает с временной ошибкой.
    old_hash = text_hash("старый отзыв до сбоя")
    await execute("""
        INSERT INTO reviews_raw (complex_id, source, review_text, text_hash, author)
        VALUES ($1, '2gis', 'старый отзыв до сбоя', $2, 'Автор')
    """, cx["id"], old_hash)

    async def collect_2gis_timeout(_cx):
        raise twogis.TransientFetchError("timeout")
    collect_2gis_timeout.__name__ = "collect_2gis"

    _patch_sources(monkeypatch, collect_2gis_timeout)
    result = await collect_one_complex(cx, None)
    assert result["failed_sources"] == ["2gis"]
    assert result["empty_sources"] == []

    sentinel_count = await fetchval(
        "SELECT count(*) FROM reviews_raw WHERE complex_id=$1 AND source='2gis' AND review_text=''", cx["id"])
    assert sentinel_count == 0  # НЕ появился ложный sentinel
    old_still_there = await fetchval(
        "SELECT count(*) FROM reviews_raw WHERE complex_id=$1 AND text_hash=$2", cx["id"], old_hash)
    assert old_still_there == 1  # старые данные не тронуты


@pytest.mark.asyncio
async def test_permanent_error_does_not_write_sentinel(cx, monkeypatch):
    """Исход 3: постоянная ошибка запроса (4xx) -> тоже НЕ sentinel (тот
    же принцип: "sentinel только после успешного ответа")."""
    import importlib
    twogis = importlib.import_module("2gis_reviews_collect")
    from reviews_pipeline import collect_one_complex
    from bot.db.pg import fetchval

    async def collect_2gis_permanent(_cx):
        raise twogis.PermanentFetchError("HTTP 403")
    collect_2gis_permanent.__name__ = "collect_2gis"

    _patch_sources(monkeypatch, collect_2gis_permanent)
    result = await collect_one_complex(cx, None)
    assert result["failed_sources"] == ["2gis"]

    sentinel_count = await fetchval(
        "SELECT count(*) FROM reviews_raw WHERE complex_id=$1 AND source='2gis'", cx["id"])
    assert sentinel_count == 0


@pytest.mark.asyncio
async def test_successful_reviews_are_inserted_no_sentinel(cx, monkeypatch):
    """Исход 4: успешное получение отзывов -> реальные строки, БЕЗ
    sentinel для этого источника (он не пустой)."""
    from reviews_pipeline import collect_one_complex
    from bot.db.pg import fetchval

    async def collect_2gis_data(_cx):
        return [{"source": "2gis", "source_entity_id": "42", "author": "Асель",
                 "review_date": date(2026, 8, 10), "rating": None,
                 "text": "Хороший двор, тихо", "source_url": "https://2gis.kz/x", "raw": None}]
    collect_2gis_data.__name__ = "collect_2gis"

    _patch_sources(monkeypatch, collect_2gis_data)
    result = await collect_one_complex(cx, None)
    assert result["inserted"] == 1
    assert result["empty_sources"] == []
    assert result["failed_sources"] == []

    real_count = await fetchval(
        "SELECT count(*) FROM reviews_raw WHERE complex_id=$1 AND source='2gis' AND review_text != ''", cx["id"])
    assert real_count == 1
    sentinel_count = await fetchval(
        "SELECT count(*) FROM reviews_raw WHERE complex_id=$1 AND source='2gis' AND review_text=''", cx["id"])
    assert sentinel_count == 0


@pytest.mark.asyncio
async def test_one_source_failure_suppresses_sibling_sentinels(cx, monkeypatch):
    """Смешанный случай: 2gis падает (временная ошибка), google_maps —
    заглушка, "успешно" вернула [] — sentinel google_maps ТОЖЕ не должен
    писаться, иначе его свежий fetched_at замаскировал бы то, что 2gis
    так и не ответил успешно ни разу (ЖК выпал бы из очереди на ~25 дней
    из-за соседнего источника-заглушки)."""
    import importlib
    twogis = importlib.import_module("2gis_reviews_collect")
    from reviews_pipeline import collect_one_complex
    from bot.db.pg import fetchval

    async def collect_2gis_timeout(_cx):
        raise twogis.TransientFetchError("timeout")
    collect_2gis_timeout.__name__ = "collect_2gis"

    async def collect_google_maps_stub(_cx):
        return []
    collect_google_maps_stub.__name__ = "collect_google_maps"

    _patch_sources(monkeypatch, collect_2gis_timeout, collect_google_maps_stub)
    result = await collect_one_complex(cx, None)
    assert result["failed_sources"] == ["2gis"]
    assert result["empty_sources"] == ["google_maps"]  # ОТВЕТИЛ, но sentinel не пишется (см. ниже)

    total_rows = await fetchval("SELECT count(*) FROM reviews_raw WHERE complex_id=$1", cx["id"])
    assert total_rows == 0  # ни 2gis, ни google_maps не оставили следа -> ЖК остаётся в очереди
