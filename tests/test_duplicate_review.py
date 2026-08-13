"""Регрессия review-UX для complex_duplicate_candidates (задача гейта 2,
п.1 после массового транслит-мерджа): bot.core.entity_resolution.
approve_duplicate_candidate()/reject_duplicate_candidate()/merge_complex_pair()
— та же реализация, что использует massовый merge_translit_dups.py
(общий код, не вторая копия). Тестовые complexes создаются и удаляются
внутри теста (не трогает реальную review-очередь — там решения принимает
человек через ритуал, не автотест).
"""
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


@pytest_asyncio.fixture
async def two_test_complexes(db):
    """Канон (2 apartment_listings под своим именем — весит больше) +
    дубль (1 apartment_listing под СВОИМ именем — именно её и должен
    перенести merge_complex_pair на имя канона). Удаляются в finally,
    даже если тест упал на середине."""
    from bot.db.pg import fetchval, execute
    canon_id = await fetchval(
        "INSERT INTO complexes (name) VALUES ('__test_canon_dup_review__') RETURNING id")
    dup_id = await fetchval(
        "INSERT INTO complexes (name) VALUES ('__test_dup_dup_review__') RETURNING id")
    await execute(
        "INSERT INTO apartment_listings (id, complex_name) VALUES ('__test_listing_canon1__', $1), "
        "('__test_listing_canon2__', $1)", '__test_canon_dup_review__')
    await execute(
        "INSERT INTO apartment_listings (id, complex_name) VALUES ('__test_listing_dup1__', $1)",
        '__test_dup_dup_review__')
    try:
        yield canon_id, dup_id
    finally:
        await execute("DELETE FROM apartment_listings WHERE id IN "
                      "('__test_listing_canon1__','__test_listing_canon2__','__test_listing_dup1__')")
        await execute("DELETE FROM complex_duplicate_candidates WHERE complex_id_a IN ($1,$2) OR complex_id_b IN ($1,$2)",
                      canon_id, dup_id)
        await execute("DELETE FROM complexes WHERE id IN ($1, $2)", canon_id, dup_id)


@pytest.mark.asyncio
async def test_approve_merges_and_marks_status(two_test_complexes):
    from bot.db.pg import fetchval, execute
    from bot.core.entity_resolution import approve_duplicate_candidate

    canon_id, dup_id = two_test_complexes
    candidate_id = await fetchval("""
        INSERT INTO complex_duplicate_candidates (complex_id_a, complex_id_b, translit_key, reason, evidence)
        VALUES ($1, $2, '__test_key__', 'no_confirming_signal', '{}'::jsonb) RETURNING id
    """, min(canon_id, dup_id), max(canon_id, dup_id))

    result = await approve_duplicate_candidate(candidate_id, approved_by="pytest")
    assert result is not None
    # канон — та сторона, у которой больше данных (apartment_listing) —
    # именно canon_id из фикстуры, независимо от того, кто из них
    # complex_id_a/complex_id_b (approve сам считает score, не берёт "a").
    assert result["canon_id"] == canon_id
    assert result["dup_id"] == dup_id
    assert result["listings_moved"] == 1  # 1 объявление было под именем ДУБЛЯ

    dup_row = await fetchval("SELECT is_garbage FROM complexes WHERE id = $1", dup_id)
    canon_row = await fetchval("SELECT is_garbage FROM complexes WHERE id = $1", canon_id)
    assert dup_row is True
    assert canon_row is not True

    status = await fetchval("SELECT status FROM complex_duplicate_candidates WHERE id = $1", candidate_id)
    assert status == "merged"

    # объявление, что было под именем дубля, реально перенесено на имя канона
    listing_name = await fetchval(
        "SELECT complex_name FROM apartment_listings WHERE id = '__test_listing_dup1__'")
    assert listing_name == "__test_canon_dup_review__"


@pytest.mark.asyncio
async def test_reject_marks_status_without_merging(two_test_complexes):
    from bot.db.pg import fetchval
    from bot.core.entity_resolution import reject_duplicate_candidate

    canon_id, dup_id = two_test_complexes
    candidate_id = await fetchval("""
        INSERT INTO complex_duplicate_candidates (complex_id_a, complex_id_b, translit_key, reason, evidence)
        VALUES ($1, $2, '__test_key__', 'no_confirming_signal', '{}'::jsonb) RETURNING id
    """, canon_id, dup_id)

    await reject_duplicate_candidate(candidate_id, rejected_by="pytest")

    status = await fetchval("SELECT status FROM complex_duplicate_candidates WHERE id = $1", candidate_id)
    assert status == "rejected"
    # ничего не смёрджено — обе стороны живы
    a_garbage = await fetchval("SELECT is_garbage FROM complexes WHERE id = $1", canon_id)
    b_garbage = await fetchval("SELECT is_garbage FROM complexes WHERE id = $1", dup_id)
    assert not a_garbage and not b_garbage


@pytest.mark.asyncio
async def test_approve_missing_candidate_returns_none(db):
    from bot.core.entity_resolution import approve_duplicate_candidate
    assert await approve_duplicate_candidate(-1, approved_by="pytest") is None
