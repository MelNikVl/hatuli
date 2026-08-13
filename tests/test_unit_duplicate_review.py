"""Регрессия review-UX для unit_duplicate_candidates (Фаза 2, юниты —
задача 2026-08-13, "review-driven режим"): bot.core.entity_resolution.
approve_unit_candidate()/reject_unit_candidate()/skip_unit_candidate() —
операционный эффект (unit_source_links / unit_duplicate_candidates.status)
И gold-label журнал (unit_match_gold_labels, migrations/051) одновременно.
Тестовые строки создаются и удаляются внутри теста (не трогает реальную
review-очередь — там решения принимает человек через ритуал).
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
async def unit_pair(db):
    """1 complex + 1 newbuild_unit + 1 apartment_listing + 1 review-
    кандидат, связывающий их — удаляются в finally, даже если тест
    упал на середине."""
    from bot.db.pg import fetchval, execute
    complex_id = await fetchval(
        "INSERT INTO complexes (name) VALUES ('__test_complex_unit_review__') RETURNING id")
    unit_id = await fetchval("""
        INSERT INTO newbuild_units (complex_id, source, source_unit_id, floor, area, price)
        VALUES ($1, 'bazis', '__test_unit_review__', 5, 60.0, 30000000) RETURNING id
    """, complex_id)
    await execute("""
        INSERT INTO apartment_listings (id, complex_name, floor, area, price)
        VALUES ('__test_listing_unit_review__', '__test_complex_unit_review__', 5, 60.0, 29500000)
    """)
    candidate_id = await fetchval("""
        INSERT INTO unit_duplicate_candidates (unit_id, listing_id, complex_id, reason, evidence)
        VALUES ($1, '__test_listing_unit_review__', $2, 'no_confirmation', $3::jsonb) RETURNING id
    """, unit_id, complex_id, '{"floor_nb": 5, "floor_al": 5, "mirror_count_nb": 1, "mirror_count_al": 1}')
    try:
        yield candidate_id, unit_id, complex_id
    finally:
        await execute("DELETE FROM unit_match_gold_labels WHERE unit_id = $1", unit_id)
        await execute("DELETE FROM unit_source_links WHERE unit_id = $1", unit_id)
        await execute("DELETE FROM unit_duplicate_candidates WHERE unit_id = $1", unit_id)
        await execute("DELETE FROM apartment_listings WHERE id = '__test_listing_unit_review__'")
        await execute("DELETE FROM newbuild_units WHERE id = $1", unit_id)
        await execute("DELETE FROM complexes WHERE id = $1", complex_id)


@pytest.mark.asyncio
async def test_approve_links_and_writes_gold_label(unit_pair):
    from bot.db.pg import fetchval
    from bot.core.entity_resolution import approve_unit_candidate

    candidate_id, unit_id, complex_id = unit_pair
    result = await approve_unit_candidate(candidate_id, approved_by="pytest")
    assert result == {"unit_id": unit_id, "listing_id": "__test_listing_unit_review__"}

    link = await fetchval(
        "SELECT match_method FROM unit_source_links WHERE unit_id=$1 AND source_id='__test_listing_unit_review__'",
        unit_id)
    assert link == "manual_review"

    status = await fetchval("SELECT status FROM unit_duplicate_candidates WHERE id=$1", candidate_id)
    assert status == "merged"

    gold = await fetchval(
        "SELECT decision FROM unit_match_gold_labels WHERE candidate_id=$1", candidate_id)
    assert gold == "approve"


@pytest.mark.asyncio
async def test_reject_marks_status_and_writes_gold_label(unit_pair):
    from bot.db.pg import fetchval
    from bot.core.entity_resolution import reject_unit_candidate

    candidate_id, unit_id, complex_id = unit_pair
    result = await reject_unit_candidate(candidate_id, rejected_by="pytest")
    assert result is not None

    status = await fetchval("SELECT status FROM unit_duplicate_candidates WHERE id=$1", candidate_id)
    assert status == "rejected"
    link = await fetchval(
        "SELECT 1 FROM unit_source_links WHERE unit_id=$1 AND source_id='__test_listing_unit_review__'", unit_id)
    assert link is None  # reject не должен создавать связь

    gold = await fetchval(
        "SELECT decision FROM unit_match_gold_labels WHERE candidate_id=$1", candidate_id)
    assert gold == "reject"


@pytest.mark.asyncio
async def test_skip_keeps_status_review_but_writes_gold_label(unit_pair):
    from bot.db.pg import fetchval
    from bot.core.entity_resolution import skip_unit_candidate

    candidate_id, unit_id, complex_id = unit_pair
    result = await skip_unit_candidate(candidate_id, skipped_by="pytest")
    assert result is not None

    status = await fetchval("SELECT status FROM unit_duplicate_candidates WHERE id=$1", candidate_id)
    assert status == "review"  # skip не резолвит кандидата — всплывёт снова

    gold = await fetchval(
        "SELECT decision FROM unit_match_gold_labels WHERE candidate_id=$1", candidate_id)
    assert gold == "skip"


@pytest.mark.asyncio
async def test_approve_missing_candidate_returns_none(db):
    from bot.core.entity_resolution import approve_unit_candidate
    result = await approve_unit_candidate(-1, approved_by="pytest")
    assert result is None
