"""Регрессия предохранителя record_source_link() от 2026-08-12 (см.
docs/entity_resolution_plan.md — 21 запись bazis/orda_invest, найденная
живой калибровкой): (source, source_id) уже в spine на ТОТ ЖЕ complex_id
-> 'already_linked', без записи в очередь (раньше уходило в review и
без конца обновлялось, хотя решение уже принято); на ДРУГОЙ complex_id
-> 'conflict', как и раньше.

Требует живой Postgres (DATABASE_URL/.env). Запуск:
venv/bin/pytest tests/test_entity_resolution_record_link.py -v
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


@pytest.mark.asyncio
async def test_already_linked_same_complex_no_queue_write(db):
    """Реальная пара из живой БД: bazis/'ADAMANT Life' уже в spine на
    complex_id=2651, confidence=1.0 (legacy_import). Пере-скор этим же
    complex_id на 0.75 (типичный для bazis — источник не отдаёт гео/
    адрес) должен вернуть 'already_linked' и НЕ создавать/обновлять
    кандидата в очереди — иначе ровно та же 21-строчная спам-ситуация,
    что и была."""
    from bot.db.pg import fetchrow, fetchval
    from bot.core.entity_resolution import record_source_link

    existing = await fetchrow(
        "SELECT complex_id, confidence FROM complex_source_links WHERE source='bazis' AND source_id='ADAMANT Life'")
    assert existing is not None, "фикстура предполагает, что bazis/'ADAMANT Life' уже в spine (см. backfill)"
    cid = existing["complex_id"]

    before = await fetchval(
        "SELECT count(*) FROM complex_source_link_candidates WHERE source='bazis' AND source_id='ADAMANT Life'")

    result = await record_source_link(
        cid, "bazis", "ADAMANT Life", confidence=0.75, method="name_exact+developer", matched_by="auto")

    after = await fetchval(
        "SELECT count(*) FROM complex_source_link_candidates WHERE source='bazis' AND source_id='ADAMANT Life'")
    spine_after = await fetchrow(
        "SELECT complex_id, confidence, match_method FROM complex_source_links WHERE source='bazis' AND source_id='ADAMANT Life'")

    assert result == "already_linked", result
    assert after == before, "record_source_link создал/обновил кандидата вместо no-op"
    # spine не тронут — остаётся исходная (более уверенная) запись
    assert spine_after["complex_id"] == cid
    assert spine_after["confidence"] == existing["confidence"]
    assert spine_after["match_method"] == "legacy_import"


@pytest.mark.asyncio
async def test_conflict_on_different_complex_still_routes_to_queue(db):
    """Другой complex_id для того же (source, source_id) — конфликт,
    как и раньше (не related к фиксу выше, но защищает от регресса той
    же правкой). Self-contained: source_id тестовый, не трогает боевые
    данные, чистит за собой в finally."""
    from bot.db.pg import fetchrow, execute, fetch
    from bot.core.entity_resolution import record_source_link

    real_complexes = await fetch("SELECT id FROM complexes WHERE is_garbage IS NOT TRUE LIMIT 2")
    assert len(real_complexes) == 2
    complex_a, complex_b = real_complexes[0]["id"], real_complexes[1]["id"]
    test_source_id = "regtest-conflict-guard"

    try:
        first = await record_source_link(
            complex_a, "bazis", test_source_id, confidence=1.0, method="test_seed", matched_by="regtest")
        assert first == "auto", first

        second = await record_source_link(
            complex_b, "bazis", test_source_id, confidence=0.9, method="test_conflict", matched_by="regtest")
        assert second == "conflict", second

        candidate = await fetchrow(
            "SELECT kind, conflict_with_complex_id FROM complex_source_link_candidates "
            "WHERE source='bazis' AND source_id=$1 AND complex_id=$2", test_source_id, complex_b)
        assert candidate is not None
        assert candidate["kind"] == "conflict"
        assert candidate["conflict_with_complex_id"] == complex_a
    finally:
        await execute("DELETE FROM complex_source_links WHERE source='bazis' AND source_id=$1", test_source_id)
        await execute("DELETE FROM complex_source_link_candidates WHERE source='bazis' AND source_id=$1", test_source_id)
