"""Регрессия "расшивки" как review-очереди (задача 2026-08-13, см.
docs/entity_resolution_plan.md — "расшивка как review-очередь"):
bot.core.entity_resolution.flag_split_candidate()/approve_split_
candidate()/reject_split_candidate()/split_auto_execution_allowed().
Тестовые complexes создаются и удаляются внутри теста (не трогает
реальную review-очередь).
"""
import json
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
async def blob_complex(db):
    """1 "блоб"-complex — удаляется в finally вместе с любыми детьми,
    которые мог создать approve_split_candidate (ищем по имени, не
    только по id — child создаётся с НОВЫМ id)."""
    from bot.db.pg import fetchval, execute
    complex_id = await fetchval(
        "INSERT INTO complexes (name, lat, lon) VALUES ('__test_blob_complex__', 51.1, 71.4) RETURNING id")
    try:
        yield complex_id
    finally:
        await execute("DELETE FROM split_candidates WHERE complex_id = $1", complex_id)
        await execute(
            "DELETE FROM complexes WHERE lower(trim(name)) LIKE lower(trim('__test_blob_complex__%'))")


@pytest.mark.asyncio
async def test_flag_creates_review_row_and_blocks_duplicate(blob_complex):
    from bot.core.entity_resolution import flag_split_candidate
    from bot.db.pg import fetchrow

    cid = await flag_split_candidate(blob_complex, "похоже на два разных дома", "pytest")
    assert cid is not None
    row = await fetchrow("SELECT * FROM split_candidates WHERE id=$1", cid)
    assert row["reason"] == "manual"
    assert row["status"] == "review"
    assert row["comment"] == "похоже на два разных дома"

    # повторная пометка того же ЖК, пока первая не разрешена — блокируется
    cid2 = await flag_split_candidate(blob_complex, "ещё раз", "pytest")
    assert cid2 is None


@pytest.mark.asyncio
async def test_approve_manual_marks_status_without_executing(blob_complex):
    from bot.core.entity_resolution import flag_split_candidate, approve_split_candidate
    from bot.db.pg import fetchval

    cid = await flag_split_candidate(blob_complex, "подозрение", "pytest")
    result = await approve_split_candidate(cid, "pytest")
    assert result == {"complex_id": blob_complex, "children": []}

    status = await fetchval("SELECT status FROM split_candidates WHERE id=$1", cid)
    assert status == "approved"


@pytest.mark.asyncio
async def test_approve_with_clusters_creates_child_with_provenance(blob_complex):
    from bot.core.entity_resolution import approve_split_candidate
    from bot.db.pg import fetchval, fetchrow, execute

    evidence = {
        "parent_name": "__test_blob_complex__",
        "clusters": [
            {"n": 10, "address": "ул. А, 1", "lat": 51.1, "lon": 71.4},
            {"n": 5, "address": "ул. Б, 2", "lat": 51.2, "lon": 71.5,
             "suggested_name": "__test_blob_complex__ 2", "tokens": ["2"]},
        ],
    }
    cid = await fetchval("""
        INSERT INTO split_candidates (complex_id, reason, evidence, matched_by)
        VALUES ($1, 'explicit_token_address', $2::jsonb, 'pytest_detector') RETURNING id
    """, blob_complex, json.dumps(evidence))

    result = await approve_split_candidate(cid, "pytest")
    assert len(result["children"]) == 1
    child_id = result["children"][0]["child_id"]
    assert result["children"][0]["action"] == "created"

    child = await fetchrow("SELECT name, provenance FROM complexes WHERE id=$1", child_id)
    assert child["name"] == "__test_blob_complex__ 2"
    prov = json.loads(child["provenance"]) if isinstance(child["provenance"], str) else child["provenance"]
    assert prov["split_from"] == blob_complex
    assert prov["matched_by"] == "unravel"

    parent = await fetchrow("SELECT provenance FROM complexes WHERE id=$1", blob_complex)
    parent_prov = json.loads(parent["provenance"]) if isinstance(parent["provenance"], str) else parent["provenance"]
    assert child_id in parent_prov["split_children"]

    await execute("DELETE FROM complexes WHERE id=$1", child_id)


@pytest.mark.asyncio
async def test_approve_existing_name_backfills_provenance_not_duplicate(blob_complex):
    """Живой кейс: Rio De Janeiro 3 (#4008) уже существовал ДО этой
    системы — approve не должен создавать дубль, только доливать
    provenance на уже существующую строку."""
    from bot.core.entity_resolution import approve_split_candidate
    from bot.db.pg import fetchval, fetchrow, execute

    existing_child_id = await fetchval(
        "INSERT INTO complexes (name) VALUES ('__test_blob_complex__ 2') RETURNING id")
    evidence = {"clusters": [
        {"n": 10, "address": "ул. А, 1"},
        {"n": 5, "address": "ул. Б, 2", "suggested_name": "__test_blob_complex__ 2", "tokens": ["2"]},
    ]}
    cid = await fetchval("""
        INSERT INTO split_candidates (complex_id, reason, evidence, matched_by)
        VALUES ($1, 'explicit_token_address', $2::jsonb, 'pytest_detector') RETURNING id
    """, blob_complex, json.dumps(evidence))

    result = await approve_split_candidate(cid, "pytest")
    assert result["children"][0]["child_id"] == existing_child_id
    assert result["children"][0]["action"] == "provenance_backfilled"

    n = await fetchval(
        "SELECT count(*) FROM complexes WHERE lower(trim(name))=lower(trim('__test_blob_complex__ 2'))")
    assert n == 1  # не создан дубль

    await execute("DELETE FROM complexes WHERE id=$1", existing_child_id)


@pytest_asyncio.fixture
async def homeportal_blob(db):
    """Родитель с токеном "4" в имени (как #4267 "Коркем III (блок 4)")
    + 2 homeportal-объекта: один совпадает с токеном родителя (должен
    остаться), другой — "чужой" токен "7" (должен переехать). Тестовые
    object_id — из высокого диапазона, не пересекается с реальными."""
    from bot.db.pg import fetchval, execute
    parent_id = await fetchval(
        "INSERT INTO complexes (name, lat, lon) VALUES ('__test_hp_parent__ (блок 4)', 51.1, 71.4) RETURNING id")
    own_obj, foreign_obj = 999990001, 999990002
    await execute("""
        INSERT INTO homeportal_objects (object_id, name, address, latitude, longitude)
        VALUES ($1, '__test_hp_parent__ (блок 4)', 'ул. Тест, 1', '51.1', '71.4')
    """, own_obj)
    await execute("""
        INSERT INTO homeportal_objects (object_id, name, address, latitude, longitude)
        VALUES ($1, '__test_hp_parent__ (блок 7)', 'ул. Тест, 1', '51.1', '71.4')
    """, foreign_obj)
    await execute("""
        INSERT INTO complex_source_links (complex_id, source, source_id, match_method, confidence, matched_by)
        VALUES ($1, 'homeportal', $2, 'manual', 1.0, 'unravel'), ($1, 'homeportal', $3, 'manual', 1.0, 'unravel')
    """, parent_id, str(own_obj), str(foreign_obj))
    try:
        yield parent_id, own_obj, foreign_obj
    finally:
        await execute("DELETE FROM complex_source_links WHERE source='homeportal' AND source_id IN ($1, $2)",
                      str(own_obj), str(foreign_obj))
        await execute("DELETE FROM homeportal_objects WHERE object_id IN ($1, $2)", own_obj, foreign_obj)
        await execute("DELETE FROM split_candidates WHERE complex_id = $1", parent_id)
        await execute(
            "DELETE FROM complexes WHERE lower(trim(name)) LIKE lower(trim('__test_hp_parent__%'))")


@pytest.mark.asyncio
async def test_approve_homeportal_blocks_relinks_foreign_object_only(homeportal_blob):
    """Живой прототип — #4267 "Коркем III (блок 4)": объект с ТЕМ ЖЕ
    токеном, что имя родителя, остаётся на месте; объект с ДРУГИМ
    токеном перелинковывается на новый child (создан по имени объекта),
    не только "создать строку" — сам complex_source_links должен
    физически поменять complex_id (в отличие от apartment_listings,
    homeportal не приджойнивается по имени сам)."""
    from bot.core.entity_resolution import approve_split_candidate
    from bot.db.pg import fetchval, fetchrow, execute

    parent_id, own_obj, foreign_obj = homeportal_blob
    evidence = {
        "parent_name": "__test_hp_parent__ (блок 4)",
        "homeportal_blocks": [
            {"object_id": own_obj, "name": "__test_hp_parent__ (блок 4)", "address": "ул. Тест, 1",
             "lat": 51.1, "lon": 71.4, "token": "4", "spot_number": None},
            {"object_id": foreign_obj, "name": "__test_hp_parent__ (блок 7)", "address": "ул. Тест, 1",
             "lat": 51.1, "lon": 71.4, "token": "7", "spot_number": None},
        ],
    }
    cid = await fetchval("""
        INSERT INTO split_candidates (complex_id, reason, evidence, matched_by)
        VALUES ($1, 'multi_source_evidence', $2::jsonb, 'pytest_detector') RETURNING id
    """, parent_id, json.dumps(evidence))

    result = await approve_split_candidate(cid, "pytest")
    assert len(result["children"]) == 1
    child = result["children"][0]
    assert child["name"] == "__test_hp_parent__ (блок 7)"
    assert child["action"] == "created"
    assert child["objects_relinked"] == 1

    own_link = await fetchrow(
        "SELECT complex_id FROM complex_source_links WHERE source='homeportal' AND source_id=$1", str(own_obj))
    assert own_link["complex_id"] == parent_id  # "свой" токен — не тронут

    foreign_link = await fetchrow(
        "SELECT complex_id, match_method FROM complex_source_links WHERE source='homeportal' AND source_id=$1",
        str(foreign_obj))
    assert foreign_link["complex_id"] == child["child_id"]  # "чужой" — перелинкован
    assert foreign_link["match_method"] == "split_detect_relink"

    await execute("DELETE FROM complexes WHERE id=$1", child["child_id"])


@pytest.mark.asyncio
async def test_reject_marks_status(blob_complex):
    from bot.core.entity_resolution import flag_split_candidate, reject_split_candidate
    from bot.db.pg import fetchval

    cid = await flag_split_candidate(blob_complex, "ложное срабатывание", "pytest")
    await reject_split_candidate(cid, "pytest")
    status = await fetchval("SELECT status FROM split_candidates WHERE id=$1", cid)
    assert status == "rejected"


@pytest.mark.asyncio
async def test_auto_gate_false_below_threshold(db):
    from bot.core.entity_resolution import split_auto_execution_allowed
    # уникальная reason-корзина, чтобы не зависеть от реальных данных БД
    assert await split_auto_execution_allowed("__test_reason_never_seen__") is False


@pytest.mark.asyncio
async def test_auto_gate_true_after_enough_precise_decisions(db):
    from bot.db.pg import fetchval, execute
    from bot.core.entity_resolution import split_auto_execution_allowed

    reason = "__test_gate_reason__"
    ids = []
    try:
        cx = await fetchval("INSERT INTO complexes (name) VALUES ('__test_gate_complex__') RETURNING id")
        for i in range(10):
            cid = await fetchval("""
                INSERT INTO split_candidates (complex_id, reason, evidence, matched_by, status, resolved_at, resolved_by)
                VALUES ($1, $2, '{}'::jsonb, 'pytest', 'approved', now(), 'pytest') RETURNING id
            """, cx, reason)
            ids.append(cid)
        assert await split_auto_execution_allowed(reason) is True

        # 1 reject роняет точность до 10/11 = 0.909 < 0.95
        rid = await fetchval("""
            INSERT INTO split_candidates (complex_id, reason, evidence, matched_by, status, resolved_at, resolved_by)
            VALUES ($1, $2, '{}'::jsonb, 'pytest', 'rejected', now(), 'pytest') RETURNING id
        """, cx, reason)
        ids.append(rid)
        assert await split_auto_execution_allowed(reason) is False
    finally:
        for i in ids:
            await execute("DELETE FROM split_candidates WHERE id=$1", i)
        await execute("DELETE FROM complexes WHERE name='__test_gate_complex__'")
