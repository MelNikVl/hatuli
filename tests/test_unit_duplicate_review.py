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
    assert result == {"unit_id": unit_id, "listing_id": "__test_listing_unit_review__", "superseded_ids": []}

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


@pytest_asyncio.fixture
async def two_units_one_listing(db):
    """1 complex + 1 apartment_listing + 2 newbuild_units, оба — review-
    кандидаты на ОДИН и тот же listing (задача 2026-08-14, "approve
    одного -> остальные кандидаты этого listing автоматически
    superseded"). Зеркало-сценарий: 2+ юнита с одинаковой планировкой
    предложены на одно объявление, только один из них — правда."""
    from bot.db.pg import fetchval, execute
    complex_id = await fetchval(
        "INSERT INTO complexes (name) VALUES ('__test_complex_unit_supersede__') RETURNING id")
    unit_a = await fetchval("""
        INSERT INTO newbuild_units (complex_id, source, source_unit_id, floor, area, price)
        VALUES ($1, 'bazis', '__test_unit_supersede_a__', 5, 60.0, 30000000) RETURNING id
    """, complex_id)
    unit_b = await fetchval("""
        INSERT INTO newbuild_units (complex_id, source, source_unit_id, floor, area, price)
        VALUES ($1, 'bazis', '__test_unit_supersede_b__', 5, 60.0, 30500000) RETURNING id
    """, complex_id)
    await execute("""
        INSERT INTO apartment_listings (id, complex_name, floor, area, price)
        VALUES ('__test_listing_unit_supersede__', '__test_complex_unit_supersede__', 5, 60.0, 29500000)
    """)
    cand_a = await fetchval("""
        INSERT INTO unit_duplicate_candidates (unit_id, listing_id, complex_id, reason, evidence)
        VALUES ($1, '__test_listing_unit_supersede__', $2, 'ambiguous_floorplan', $3::jsonb) RETURNING id
    """, unit_a, complex_id, '{"floor_nb": 5, "floor_al": 5, "mirror_count_nb": 2, "mirror_count_al": 1}')
    cand_b = await fetchval("""
        INSERT INTO unit_duplicate_candidates (unit_id, listing_id, complex_id, reason, evidence)
        VALUES ($1, '__test_listing_unit_supersede__', $2, 'ambiguous_floorplan', $3::jsonb) RETURNING id
    """, unit_b, complex_id, '{"floor_nb": 5, "floor_al": 5, "mirror_count_nb": 2, "mirror_count_al": 1}')
    try:
        yield cand_a, cand_b, unit_a, unit_b, complex_id
    finally:
        await execute("DELETE FROM unit_match_gold_labels WHERE unit_id IN ($1, $2)", unit_a, unit_b)
        await execute("DELETE FROM unit_source_links WHERE unit_id IN ($1, $2)", unit_a, unit_b)
        await execute("DELETE FROM unit_duplicate_candidates WHERE unit_id IN ($1, $2)", unit_a, unit_b)
        await execute("DELETE FROM apartment_listings WHERE id = '__test_listing_unit_supersede__'")
        await execute("DELETE FROM newbuild_units WHERE id IN ($1, $2)", unit_a, unit_b)
        await execute("DELETE FROM complexes WHERE id = $1", complex_id)


@pytest.mark.asyncio
async def test_approve_supersedes_other_candidates_same_listing(two_units_one_listing):
    """Approve кандидата A -> кандидат B (тот же listing_id, ещё был в
    review) автоматически status='superseded' (не 'rejected' — другое
    происхождение решения) + gold label decision='reject' на B
    ("разные квартиры" — правда, раз объявление продаёт A)."""
    from bot.db.pg import fetchval
    from bot.core.entity_resolution import approve_unit_candidate

    cand_a, cand_b, unit_a, unit_b, complex_id = two_units_one_listing
    result = await approve_unit_candidate(cand_a, approved_by="pytest")
    assert result["unit_id"] == unit_a
    assert result["superseded_ids"] == [cand_b]

    status_a = await fetchval("SELECT status FROM unit_duplicate_candidates WHERE id=$1", cand_a)
    status_b = await fetchval("SELECT status FROM unit_duplicate_candidates WHERE id=$1", cand_b)
    assert status_a == "merged"
    assert status_b == "superseded"

    # B не должен получить связь в unit_source_links — он проиграл, не подтверждён
    link_b = await fetchval(
        "SELECT 1 FROM unit_source_links WHERE unit_id=$1 AND source_id='__test_listing_unit_supersede__'", unit_b)
    assert link_b is None

    gold_b = await fetchval(
        "SELECT decision FROM unit_match_gold_labels WHERE candidate_id=$1", cand_b)
    assert gold_b == "reject"


@pytest.mark.asyncio
async def test_unit_id_listing_id_unique_constraint(two_units_one_listing):
    """Регрессия задачи 2026-08-14 ("Проверить истинные дубли... добавить
    UNIQUE-констрейнт") — повторная пара (unit_id, listing_id) должна
    падать на уровне БД, не создавать вторую review-строку."""
    import asyncpg
    from bot.db.pg import execute

    cand_a, cand_b, unit_a, unit_b, complex_id = two_units_one_listing
    with pytest.raises(asyncpg.UniqueViolationError):
        await execute("""
            INSERT INTO unit_duplicate_candidates (unit_id, listing_id, complex_id, reason, evidence)
            VALUES ($1, '__test_listing_unit_supersede__', $2, 'no_confirmation', '{}'::jsonb)
        """, unit_a, complex_id)
