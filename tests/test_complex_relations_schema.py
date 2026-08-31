"""tests/test_complex_relations_schema.py — задача 2026-08-30, "Complex
Identity layer", Phase 4/5 — schema-only constraints для migrations/095
(complex_relations) и migrations/096 (listing_complex_resolution_log).
Synthetic fixtures (тот же паттерн, что tests/test_property_merge.py),
удаляются в finally.

НЕ запущено локально в этой сессии против общей dev/prod Postgres —
задача явно: "Никаких production writes кроме schema migration после
отдельного PR/merge approval" — даже создание пустых таблиц через
init_pool()'s auto-apply здесь сознательно отложено до реального
merge. Эти тесты рассчитаны на CI (свежий эфемерный Postgres в
.github/workflows/ci.yml — не разделяемая среда, миграция там
применяется с нуля, не "заранее"), а не на локальный прогон сейчас."""
import os
import sys
from datetime import datetime, timezone

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


async def _make_complex(name: str) -> int:
    from bot.db.pg import fetchval
    return await fetchval("INSERT INTO complexes (name) VALUES ($1) RETURNING id", name)


async def _cleanup_complexes(complex_ids):
    from bot.db.pg import execute
    cids = [c for c in complex_ids if c]
    await execute("DELETE FROM complex_relations WHERE complex_id_a = ANY($1::int[]) OR complex_id_b = ANY($1::int[])", cids)
    await execute("DELETE FROM complexes WHERE id = ANY($1::int[])", cids)


async def _cleanup_listings(listing_ids, complex_ids):
    from bot.db.pg import execute
    lids = list(listing_ids)
    await execute("DELETE FROM listing_complex_resolution_log WHERE listing_id = ANY($1::text[])", lids)
    await execute("DELETE FROM apartment_listings WHERE id = ANY($1::text[])", lids)
    await _cleanup_complexes(complex_ids)


# ── complex_relations ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_complex_relations_insert_and_read(db):
    from bot.db.pg import execute, fetchrow
    a = b = None
    try:
        a = await _make_complex("__test_cr_a__")
        b = await _make_complex("__test_cr_b__")
        lo, hi = min(a, b), max(a, b)
        await execute(
            "INSERT INTO complex_relations (complex_id_a, complex_id_b, relation_type, confidence, "
            "evidence, reviewed_by, reviewed_at, methodology_version) "
            "VALUES ($1,$2,'sibling_phase',0.8,'{}'::jsonb,'tester',now(),'test_v1')",
            lo, hi,
        )
        row = await fetchrow("SELECT * FROM complex_relations WHERE complex_id_a=$1 AND complex_id_b=$2", lo, hi)
        assert row["relation_type"] == "sibling_phase"
        assert float(row["confidence"]) == pytest.approx(0.8)  # NUMERIC -> Decimal, float(0.8) != Decimal('0.8') exactly
    finally:
        await _cleanup_complexes([a, b])


@pytest.mark.asyncio
async def test_complex_relations_rejects_reversed_canonical_order(db):
    """complex_id_a > complex_id_b -> CHECK constraint violation, не
    молчаливая перестановка — вызывающий код обязан сам канонизировать
    порядок ПЕРЕД insert (min/max), схема только СТРОГО проверяет, не
    исправляет за него."""
    import asyncpg
    from bot.db.pg import execute
    a = b = None
    try:
        a = await _make_complex("__test_cr_order_a__")
        b = await _make_complex("__test_cr_order_b__")
        lo, hi = min(a, b), max(a, b)
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await execute(
                "INSERT INTO complex_relations (complex_id_a, complex_id_b, relation_type, confidence, "
                "evidence, reviewed_by, reviewed_at, methodology_version) "
                "VALUES ($1,$2,'sibling_phase',0.8,'{}'::jsonb,'tester',now(),'test_v1')",
                hi, lo,  # намеренно перевёрнуто
            )
    finally:
        await _cleanup_complexes([a, b])


@pytest.mark.asyncio
async def test_complex_relations_rejects_invalid_relation_type(db):
    """'ambiguous' НЕ допустимое значение — задача, явно: не факт
    relation, а review-состояние вне этой таблицы."""
    import asyncpg
    from bot.db.pg import execute
    a = b = None
    try:
        a = await _make_complex("__test_cr_invalid_a__")
        b = await _make_complex("__test_cr_invalid_b__")
        lo, hi = min(a, b), max(a, b)
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await execute(
                "INSERT INTO complex_relations (complex_id_a, complex_id_b, relation_type, confidence, "
                "evidence, reviewed_by, reviewed_at, methodology_version) "
                "VALUES ($1,$2,'ambiguous',0.5,'{}'::jsonb,'tester',now(),'test_v1')",
                lo, hi,
            )
    finally:
        await _cleanup_complexes([a, b])


@pytest.mark.asyncio
async def test_complex_relations_unique_pair_one_row_per_pair(db):
    """UNIQUE(complex_id_a, complex_id_b) без relation_type в ключе —
    пара ЖК имеет РОВНО одно актуальное отношение; повторный INSERT той
    же пары (даже с другим relation_type) -> UniqueViolationError.
    Исправление существующей разметки — UPDATE, не второй INSERT (см.
    докстринг миграции)."""
    import asyncpg
    from bot.db.pg import execute
    a = b = None
    try:
        a = await _make_complex("__test_cr_unique_a__")
        b = await _make_complex("__test_cr_unique_b__")
        lo, hi = min(a, b), max(a, b)
        await execute(
            "INSERT INTO complex_relations (complex_id_a, complex_id_b, relation_type, confidence, "
            "evidence, reviewed_by, reviewed_at, methodology_version) "
            "VALUES ($1,$2,'sibling_phase',0.8,'{}'::jsonb,'tester',now(),'test_v1')",
            lo, hi,
        )
        with pytest.raises(asyncpg.exceptions.UniqueViolationError):
            await execute(
                "INSERT INTO complex_relations (complex_id_a, complex_id_b, relation_type, confidence, "
                "evidence, reviewed_by, reviewed_at, methodology_version) "
                "VALUES ($1,$2,'duplicate_same_complex',0.9,'{}'::jsonb,'tester2',now(),'test_v1')",
                lo, hi,
            )
    finally:
        await _cleanup_complexes([a, b])


@pytest.mark.asyncio
async def test_complex_relations_update_corrects_existing_row(db):
    """Явно задокументированный путь исправления — UPDATE той же
    (complex_id_a, complex_id_b) строки, не второй INSERT."""
    from bot.db.pg import execute, fetchrow
    a = b = None
    try:
        a = await _make_complex("__test_cr_update_a__")
        b = await _make_complex("__test_cr_update_b__")
        lo, hi = min(a, b), max(a, b)
        await execute(
            "INSERT INTO complex_relations (complex_id_a, complex_id_b, relation_type, confidence, "
            "evidence, reviewed_by, reviewed_at, methodology_version) "
            "VALUES ($1,$2,'sibling_phase',0.5,'{}'::jsonb,'tester',now(),'test_v1')",
            lo, hi,
        )
        await execute(
            "UPDATE complex_relations SET relation_type='duplicate_same_complex', confidence=0.95, "
            "reviewed_by='tester2', reviewed_at=now() WHERE complex_id_a=$1 AND complex_id_b=$2",
            lo, hi,
        )
        row = await fetchrow("SELECT * FROM complex_relations WHERE complex_id_a=$1 AND complex_id_b=$2", lo, hi)
        assert row["relation_type"] == "duplicate_same_complex"
        assert float(row["confidence"]) == pytest.approx(0.95)  # NUMERIC -> Decimal
        # ровно одна строка -> UPDATE, не accumulate
        from bot.db.pg import fetchval
        n = await fetchval("SELECT count(*) FROM complex_relations WHERE complex_id_a=$1 AND complex_id_b=$2", lo, hi)
        assert n == 1
    finally:
        await _cleanup_complexes([a, b])


@pytest.mark.asyncio
async def test_complex_relations_no_cascade_delete(db):
    """Удаление complexes-строки, на которую ссылается уже проверенная
    relation, ДОЛЖНО быть заблокировано (RESTRICT/NO ACTION), не
    молча каскадно стереть relation вместе с ней."""
    import asyncpg
    from bot.db.pg import execute
    a = b = None
    try:
        a = await _make_complex("__test_cr_cascade_a__")
        b = await _make_complex("__test_cr_cascade_b__")
        lo, hi = min(a, b), max(a, b)
        await execute(
            "INSERT INTO complex_relations (complex_id_a, complex_id_b, relation_type, confidence, "
            "evidence, reviewed_by, reviewed_at, methodology_version) "
            "VALUES ($1,$2,'sibling_phase',0.8,'{}'::jsonb,'tester',now(),'test_v1')",
            lo, hi,
        )
        with pytest.raises((asyncpg.exceptions.ForeignKeyViolationError,)):
            await execute("DELETE FROM complexes WHERE id=$1", lo)
    finally:
        await _cleanup_complexes([a, b])


# ── listing_complex_resolution_log ──────────────────────────────────────

@pytest.mark.asyncio
async def test_listing_complex_resolution_log_append_only_multiple_attempts(db):
    """Один listing может быть резолвлен несколько раз (напр. эвристика,
    потом человеком) — КАЖДАЯ попытка своя строка, не upsert."""
    from bot.db.pg import execute, fetchval

    lid = "__test_lcrl_listing__"
    cid = None
    try:
        cid = await _make_complex("__test_lcrl_complex__")
        await execute(
            "INSERT INTO apartment_listings (id, url, complex_name) VALUES ($1,$2,$3)",
            lid, f"https://krisha.kz/test/{lid}", "Тестовый ЖК",
        )
        for method, tier in (("name_exact_match", "A"), ("human_review", "A")):
            await execute(
                "INSERT INTO listing_complex_resolution_log (listing_id, complex_id, resolution_method, "
                "confidence_tier, resolved_at, complex_name_at_resolution, evidence, resolver_version) "
                "VALUES ($1,$2,$3,$4,now(),$5,'{}'::jsonb,'test_v1')",
                lid, cid, method, tier, "Тестовый ЖК",
            )
        n = await fetchval("SELECT count(*) FROM listing_complex_resolution_log WHERE listing_id=$1", lid)
        assert n == 2
    finally:
        await _cleanup_listings([lid], [cid])


@pytest.mark.asyncio
async def test_listing_complex_resolution_log_rejects_bad_confidence_tier(db):
    import asyncpg
    from bot.db.pg import execute

    lid = "__test_lcrl_badtier_listing__"
    cid = None
    try:
        cid = await _make_complex("__test_lcrl_badtier_complex__")
        await execute(
            "INSERT INTO apartment_listings (id, url, complex_name) VALUES ($1,$2,$3)",
            lid, f"https://krisha.kz/test/{lid}", "Тестовый ЖК",
        )
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await execute(
                "INSERT INTO listing_complex_resolution_log (listing_id, complex_id, resolution_method, "
                "confidence_tier, resolved_at, complex_name_at_resolution, evidence, resolver_version) "
                "VALUES ($1,$2,'x','Z',now(),'y','{}'::jsonb,'test_v1')",
                lid, cid,
            )
    finally:
        await _cleanup_listings([lid], [cid])
