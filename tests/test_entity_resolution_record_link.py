"""Регрессия предохранителя record_source_link() от 2026-08-12 (см.
docs/entity_resolution_plan.md — 21 запись bazis/orda_invest, найденная
живой калибровкой): (source, source_id) уже в spine на ТОТ ЖЕ complex_id
-> 'already_linked', без записи в очередь (раньше уходило в review и
без конца обновлялось, хотя решение уже принято); на ДРУГОЙ complex_id
-> 'conflict', как и раньше.

Требует Postgres (DATABASE_URL/.env), но НЕ конкретные реальные строки —
оба теста (2026-08-16, "P0 — Integrity") переведены на свои
синтетические complexes/complex_source_links, самодостаточны на пустой
БД. Запуск: venv/bin/pytest tests/test_entity_resolution_record_link.py -v
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
    """Мотивация — реальная пара из живой БД (bazis/'ADAMANT Life' на
    complex_id=2651, confidence=1.0, legacy_import — см. модульный
    докстринг про 21-строчную спам-ситуацию), но сама ПРОВЕРКА не
    завязана на эту конкретную пару: предохранитель в record_source_
    link() смотрит только на "(source, source_id) уже в spine на ТОТ ЖЕ
    complex_id?", ему всё равно, legacy_import это или свежий seed
    (задача 2026-08-16, "P0 — Integrity", закрытие --deselect в CI —
    'ADAMANT Life' на пустой CI-БД не существует). Синтетический seed
    ниже воспроизводит тот же сценарий: spine уже содержит (source,
    source_id) на confidence=1.0/'legacy_import', пере-скор тем же
    complex_id на более низкий confidence (типично для bazis — источник
    не отдаёт гео/адрес) должен вернуть 'already_linked' и НЕ создавать/
    обновлять кандидата в очереди."""
    from bot.db.pg import fetchrow, fetchval, execute
    from bot.core.entity_resolution import record_source_link

    cid = await fetchval(
        "INSERT INTO complexes (name, is_garbage) VALUES ('__test_erl_linked__', FALSE) RETURNING id")
    test_source_id = "regtest-already-linked-guard"
    await execute(
        "INSERT INTO complex_source_links (complex_id, source, source_id, confidence, match_method, matched_by) "
        "VALUES ($1, 'bazis', $2, 1.0, 'legacy_import', 'seed')", cid, test_source_id)

    try:
        existing = await fetchrow(
            "SELECT complex_id, confidence FROM complex_source_links WHERE source='bazis' AND source_id=$1",
            test_source_id)

        before = await fetchval(
            "SELECT count(*) FROM complex_source_link_candidates WHERE source='bazis' AND source_id=$1",
            test_source_id)

        result = await record_source_link(
            cid, "bazis", test_source_id, confidence=0.75, method="name_exact+developer", matched_by="auto")

        after = await fetchval(
            "SELECT count(*) FROM complex_source_link_candidates WHERE source='bazis' AND source_id=$1",
            test_source_id)
        spine_after = await fetchrow(
            "SELECT complex_id, confidence, match_method FROM complex_source_links WHERE source='bazis' AND source_id=$1",
            test_source_id)

        assert result == "already_linked", result
        assert after == before, "record_source_link создал/обновил кандидата вместо no-op"
        # spine не тронут — остаётся исходная (более уверенная) запись
        assert spine_after["complex_id"] == cid
        assert spine_after["confidence"] == existing["confidence"]
        assert spine_after["match_method"] == "legacy_import"
    finally:
        await execute("DELETE FROM complex_source_links WHERE source='bazis' AND source_id=$1", test_source_id)
        await execute("DELETE FROM complex_source_link_candidates WHERE source='bazis' AND source_id=$1", test_source_id)
        await execute("DELETE FROM complexes WHERE id=$1", cid)


@pytest.mark.asyncio
async def test_conflict_on_different_complex_still_routes_to_queue(db):
    """Другой complex_id для того же (source, source_id) — конфликт,
    как и раньше (не related к фиксу выше, но защищает от регресса той
    же правкой). Self-contained: source_id тестовый, не трогает боевые
    данные, чистит за собой в finally.

    complex_a/complex_b — СВОИ синтетические complexes (задача
    2026-08-16, "P0 — Integrity", закрытие --deselect в CI), не 'любые
    два реальных из БД': тест по своей же формулировке не завязан на
    конкретные ЖК, ему нужны любые два РАЗНЫХ complex_id — раньше брал
    их из живой таблицы (SELECT ... LIMIT 2), что на пустой CI-БД давало
    0 строк вместо 2. Логика конфликта одинакова для любых двух id."""
    from bot.db.pg import fetchrow, fetchval, execute
    from bot.core.entity_resolution import record_source_link

    complex_a = await fetchval(
        "INSERT INTO complexes (name, is_garbage) VALUES ('__test_erl_a__', FALSE) RETURNING id")
    complex_b = await fetchval(
        "INSERT INTO complexes (name, is_garbage) VALUES ('__test_erl_b__', FALSE) RETURNING id")
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
        await execute("DELETE FROM complexes WHERE id = ANY($1::int[])", [complex_a, complex_b])
