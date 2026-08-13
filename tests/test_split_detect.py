"""Регрессия split_detect.py — многоуровневый детектор расшивки
(задача 2026-08-13, второй проход, см. docs/entity_resolution_plan.md
— "расшивка как review-очередь"). Чистые функции (_explicit_marker_
token/cluster_listings/gather_krisha_deadlines/decide_candidate) —
без БД; gather_homeportal_blocks — против живой БД (тот же паттерн,
что остальные тесты гейта Фазы 2)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio
from dotenv import load_dotenv

load_dotenv()

from split_detect import (
    _explicit_marker_token, cluster_listings, gather_krisha_deadlines,
    gather_apartment_listings_evidence, decide_candidate,
)

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")


def test_explicit_marker_token_queue():
    assert _explicit_marker_token("2-я очередь дома") == "2"
    assert _explicit_marker_token("Очередь № 3, сдача 2025") == "3"


def test_explicit_marker_token_block_letter():
    assert _explicit_marker_token("F блок, 5 этаж") == "block:f"
    assert _explicit_marker_token("корпус D, окна во двор") == "block:d"


def test_explicit_marker_token_none_on_plain_text():
    assert _explicit_marker_token("3-комнатная квартира, 62 м², 6/9 этаж") is None


def test_explicit_marker_token_pyatno_kvartal_not_a_token():
    # та же защита, что _phase_token — "пятно"/"квартал" не разграничитель
    assert _explicit_marker_token("пятно 6, 2 очередь") is None
    assert _explicit_marker_token("квартал 5") is None


def test_cluster_listings_two_far_clusters():
    listings = [
        {"id": "a1", "lat": 51.10, "lon": 71.40},
        {"id": "a2", "lat": 51.1005, "lon": 71.4005},
        {"id": "b1", "lat": 51.20, "lon": 71.50},  # >1км от a-группы
        {"id": "b2", "lat": 51.2005, "lon": 71.5005},
    ]
    clusters = cluster_listings(listings)
    assert len(clusters) == 2
    assert {len(c) for c in clusters} == {2, 2}


def test_cluster_listings_one_cluster_when_close():
    listings = [{"id": i, "lat": 51.10 + i * 0.0001, "lon": 71.40} for i in range(4)]
    clusters = cluster_listings(listings)
    assert len(clusters) == 1


def test_gather_krisha_deadlines_requires_2_plus():
    assert gather_krisha_deadlines({"source_info": {"krisha": {"deadlines_by_queue": None}}}) is None
    assert gather_krisha_deadlines({"source_info": {}}) is None
    one = {"source_info": {"krisha": {"deadlines_by_queue": [{"label": "x", "deadline": "y"}]}}}
    assert gather_krisha_deadlines(one) is None  # 1 запись — не сигнал (нечего сравнивать)
    two = {"source_info": {"krisha": {"deadlines_by_queue": [
        {"label": "Первая очередь", "deadline": "2020"}, {"label": "Вторая очередь", "deadline": "2022"}]}}}
    assert len(gather_krisha_deadlines(two)) == 2


def test_gather_apartment_listings_evidence_explicit_token():
    listings = [
        {"id": i, "lat": 51.10, "lon": 71.40, "title": None, "description": None} for i in range(5)
    ] + [
        {"id": 100 + i, "lat": 51.11, "lon": 71.42, "title": "2-я очередь, отличная квартира",
         "description": None} for i in range(3)
    ]
    ev = gather_apartment_listings_evidence("test complex", listings)
    assert ev is not None
    assert ev["has_explicit_token"] is True
    assert ev["clusters"][1]["suggested_name"] == "test complex 2"


def test_gather_apartment_listings_evidence_none_below_min_listings():
    listings = [{"id": 1, "lat": 51.1, "lon": 71.4, "title": None, "description": None}]
    assert gather_apartment_listings_evidence("x", listings) is None


def test_decide_candidate_explicit_token_wins():
    al_ev = {"clusters": [{"n": 5}, {"n": 3, "suggested_name": "x 2"}], "has_explicit_token": True}
    reason, ev = decide_candidate(al_ev, None, None, [], None, None, "x")
    assert reason == "explicit_token_address"
    assert ev["apartment_listings_geo_clusters"] == al_ev["clusters"]


def test_decide_candidate_multi_source_when_no_explicit_token():
    al_ev = {"clusters": [{"n": 5}, {"n": 3}], "has_explicit_token": False}
    reason, ev = decide_candidate(al_ev, None, None, [], None, None, "x")
    assert reason == "multi_source_evidence"


def test_decide_candidate_krisha_deadlines_alone_is_multi_source():
    deadlines = [{"label": "Первая очередь", "deadline": "2020"}, {"label": "Вторая очередь", "deadline": "2022"}]
    reason, ev = decide_candidate(None, deadlines, None, [], None, None, "x")
    assert reason == "multi_source_evidence"
    assert ev["krisha_deadlines"] == deadlines


def test_decide_candidate_homeportal_tokens_never_upgrade_to_explicit():
    # решение заказчика: даже явный токен из homeportal остаётся multi_source_evidence
    blocks = [{"name": "a", "token": "1"}, {"name": "b", "token": "2"}]
    reason, ev = decide_candidate(None, None, blocks, ["1", "2"], None, None, "x")
    assert reason == "multi_source_evidence"
    assert ev["explicit_tokens"] == ["1", "2"]


def test_decide_candidate_none_when_no_signal_at_all():
    assert decide_candidate(None, None, None, [], None, None, "x") is None


@pytest_asyncio.fixture
async def db():
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    yield
    await close_pool()


@pytest.mark.asyncio
async def test_gather_homeportal_blocks_multi_token_complex(db):
    """Раньше проверялось на живом complex_id=1193 (UIA.DARYN, 9
    homeportal-объектов A-H,M) — но именно ЭТОТ живой кейс стал первым
    umbrella-split (задача 2026-08-13, "модель зонтик/дом"): #1193
    сузился до одного объекта (блок B), 8 остальных разъехались по
    новым complex_id. Тест на мутирующие живые данные — хрупкость,
    какую сам этот прогон и продемонстрировал; переведён на
    самодостаточную фикстуру."""
    from bot.db.pg import fetch, fetchval, execute
    cid = await fetchval(
        "INSERT INTO complexes (name, lat, lon) VALUES ('__test_hp_multi_token__', 51.1, 71.4) RETURNING id")
    obj_a, obj_b = 999990101, 999990102
    try:
        await execute("""
            INSERT INTO homeportal_objects (object_id, name, address, latitude, longitude)
            VALUES ($1, '__test_hp_multi_token__ A', 'ул. Тест, 1', '51.1', '71.4'),
                   ($2, '__test_hp_multi_token__ B', 'ул. Тест, 2', '51.2', '71.5')
        """, obj_a, obj_b)
        await execute("""
            INSERT INTO complex_source_links (complex_id, source, source_id, match_method, confidence, matched_by)
            VALUES ($1, 'homeportal', $2, 'manual', 1.0, 'pytest'),
                   ($1, 'homeportal', $3, 'manual', 1.0, 'pytest')
        """, cid, str(obj_a), str(obj_b))

        from split_detect import gather_homeportal_blocks
        blocks, tokens = await gather_homeportal_blocks(cid, fetch)
        assert blocks is not None
        assert len(blocks) == 2
        assert len(set(tokens)) == 2
    finally:
        await execute("DELETE FROM complex_source_links WHERE source='homeportal' AND source_id IN ($1, $2)",
                      str(obj_a), str(obj_b))
        await execute("DELETE FROM homeportal_objects WHERE object_id IN ($1, $2)", obj_a, obj_b)
        await execute("DELETE FROM complexes WHERE id=$1", cid)


@pytest.mark.asyncio
async def test_gather_homeportal_blocks_none_below_2_objects(db):
    from bot.db.pg import fetch
    from split_detect import gather_homeportal_blocks
    blocks, tokens = await gather_homeportal_blocks(-1, fetch)
    assert blocks is None
    assert tokens == []
