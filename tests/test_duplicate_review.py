"""Legacy duplicate approval cannot bypass the reviewed redirect workflow."""
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
    """Two records and original source names, removed in finally."""
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
async def test_legacy_approve_preserves_candidate_and_source_names(two_test_complexes):
    from bot.db.pg import fetchval, execute
    from bot.core.entity_resolution import approve_duplicate_candidate

    canon_id, dup_id = two_test_complexes
    candidate_id = await fetchval("""
        INSERT INTO complex_duplicate_candidates (complex_id_a, complex_id_b, translit_key, reason, evidence)
        VALUES ($1, $2, '__test_key__', 'no_confirming_signal', '{}'::jsonb) RETURNING id
    """, min(canon_id, dup_id), max(canon_id, dup_id))

    from bot.core.complex_identity_review import ComplexIdentityReviewError
    with pytest.raises(ComplexIdentityReviewError) as error:
        await approve_duplicate_candidate(candidate_id, approved_by="pytest")
    assert error.value.code == "preview_required"

    dup_row = await fetchval("SELECT is_garbage FROM complexes WHERE id = $1", dup_id)
    canon_row = await fetchval("SELECT is_garbage FROM complexes WHERE id = $1", canon_id)
    assert dup_row is not True
    assert canon_row is not True

    status = await fetchval("SELECT status FROM complex_duplicate_candidates WHERE id = $1", candidate_id)
    assert status == "review"

    # The original source spelling remains a historical observation.
    listing_name = await fetchval(
        "SELECT complex_name FROM apartment_listings WHERE id = '__test_listing_dup1__'")
    assert listing_name == "__test_dup_dup_review__"


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
async def test_legacy_approve_requires_preview_before_any_database_access():
    from bot.core.entity_resolution import approve_duplicate_candidate
    from bot.core.complex_identity_review import ComplexIdentityReviewError
    with pytest.raises(ComplexIdentityReviewError) as error:
        await approve_duplicate_candidate(-1, approved_by="pytest")
    assert error.value.code == "preview_required"
