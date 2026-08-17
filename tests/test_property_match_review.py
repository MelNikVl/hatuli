"""Регрессия для задачи 2026-08-17, "Property Identity — photo evidence +
admin review queue", части C/D/E: corroborating_methods, soft conflicts,
bot/identity/review_decisions.py, /admin/property-match-review. Тестовые
строки — '__test_...__' id, удаляются в finally (тот же паттерн, что
tests/test_property_identity_incremental.py)."""
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


async def _insert_listing(lid, address=None, floor=None, area=None, rooms=None, complex_name=None,
                           price=None, seller_name=None, is_active=True, photos=None,
                           duplicate_of=None, is_duplicate=False, dup_match=None):
    import json as _json
    from bot.db.pg import execute
    await execute(
        """
        INSERT INTO apartment_listings (id, url, address, floor, area, rooms, complex_name, price,
                                         seller_name, is_active, photos, duplicate_of, is_duplicate, dup_match)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, $12, $13, $14)
        ON CONFLICT (id) DO UPDATE SET address=$3, floor=$4, area=$5, rooms=$6, complex_name=$7, price=$8,
            seller_name=$9, is_active=$10, photos=$11::jsonb, duplicate_of=$12, is_duplicate=$13, dup_match=$14
        """,
        lid, f"https://krisha.kz/test/{lid}", address, floor, area, rooms, complex_name, price,
        seller_name, is_active, _json.dumps(photos or []), duplicate_of, is_duplicate, dup_match,
    )


async def _cleanup(*listing_ids, complex_ids=(), property_ids=()):
    from bot.db.pg import execute
    await execute("DELETE FROM property_match_review_log WHERE listing_id = ANY($1::text[])", list(listing_ids))
    await execute("DELETE FROM property_candidate_photo_evidence WHERE candidate_id IN "
                  "(SELECT candidate_id FROM property_match_candidates WHERE listing_id = ANY($1::text[]))",
                  list(listing_ids))
    await execute("DELETE FROM property_match_candidates WHERE listing_id = ANY($1::text[])", list(listing_ids))
    await execute("DELETE FROM listing_photo_fingerprints WHERE listing_id = ANY($1::text[])", list(listing_ids))
    await execute("DELETE FROM property_listings WHERE listing_id = ANY($1::text[])", list(listing_ids))
    if property_ids:
        await execute("DELETE FROM properties WHERE property_id = ANY($1::int[])", list(property_ids))
    await execute("DELETE FROM apartment_listings WHERE id = ANY($1::text[])", list(listing_ids))
    if complex_ids:
        await execute("DELETE FROM complexes WHERE id = ANY($1::int[])", list(complex_ids))


# ── D. house_number/price mismatch перестают быть безусловным auto-reject ──

@pytest.mark.asyncio
async def test_house_number_mismatch_alone_stays_pending_reviewable(db):
    from bot.identity.property_linker import link_listing_to_property, compute_address_hash

    lid_a, lid_b = "__test_pmr_hn_a__", "__test_pmr_hn_b__"
    await _insert_listing(lid_a, address="Улица Х, 5", floor=5, area=45.0, rooms=2)
    await _insert_listing(lid_b, address="Улица Х, 7", floor=5, area=45.0, rooms=2)  # тот же этаж/площадь/комнаты, другой дом
    h1 = compute_address_hash("Улица Х, 5", 5, 45.0)
    h2 = compute_address_hash("Улица Х, 7", 5, 45.0)
    try:
        await link_listing_to_property({"id": lid_a, "address": "Улица Х, 5", "floor": 5, "area": 45.0,
                                         "rooms": 2, "complex_name": None})
        r2 = await link_listing_to_property({"id": lid_b, "address": "Улица Х, 7", "floor": 5, "area": 45.0,
                                              "rooms": 2, "complex_name": None})
        # complex_name=None -> fuzzy не считается (нужен complex_id), но
        # dedup/exact тоже не сработают тут — просто проверяем, что
        # bootstrap отработал без ошибок под этим сценарием (детальная
        # проверка soft-conflict поведения — в следующем тесте, напрямую
        # через _build_candidate_record, там же, где раньше это тестировал
        # rooms-mismatch тест).
        assert r2["method"] == "bootstrap"
    finally:
        from bot.db.pg import execute
        await execute("DELETE FROM property_match_candidates WHERE listing_id = ANY($1::text[])", [lid_a, lid_b])
        await execute("DELETE FROM property_listings WHERE listing_id = ANY($1::text[])", [lid_a, lid_b])
        await execute("DELETE FROM properties WHERE address_hash = ANY($1::text[])", [h1, h2])
        await execute("DELETE FROM apartment_listings WHERE id = ANY($1::text[])", [lid_a, lid_b])


@pytest.mark.asyncio
async def test_house_number_mismatch_via_exact_hash_pair_stays_pending(db):
    """Прямой тест _build_candidate_record — house_number mismatch БЕЗ
    rooms mismatch -> status='pending' (НЕ 'rejected', задача D)."""
    from bot.identity.property_linker import _build_candidate_record

    listing = {"id": "L1", "address": "Абая, 10", "floor": 5, "area": 45.0, "rooms": 2,
               "seller_name": None, "price": 20000000, "first_seen": None, "archived_at": None}
    candidate = {"id": "L2", "property_id": 1, "address": "Абая, 25", "floor": 5, "area": 45.0,
                 "rooms": 2, "seller_name": None, "price": 20500000, "first_seen": None, "archived_at": None}
    record = _build_candidate_record(listing, candidate, "fuzzy", 0.8)
    assert any("house_number" in r for r in record["conflict_reasons"])
    assert record["status"] == "pending"  # НЕ rejected — soft conflict


def test_price_diff_over_30pct_alone_stays_pending():
    from bot.identity.property_linker import _build_candidate_record

    listing = {"id": "L1", "address": "Абая, 10", "floor": 5, "area": 45.0, "rooms": 2,
               "seller_name": None, "price": 20000000, "first_seen": None, "archived_at": None}
    candidate = {"id": "L2", "property_id": 1, "address": "Абая, 10", "floor": 5, "area": 45.0,
                 "rooms": 2, "seller_name": None, "price": 30000000, "first_seen": None, "archived_at": None}
    record = _build_candidate_record(listing, candidate, "exact_hash", 0.9)
    assert any("price differs" in r for r in record["conflict_reasons"])
    assert record["status"] == "pending"


def test_rooms_mismatch_combined_with_house_number_still_hard_rejects():
    """rooms mismatch ОСТАЁТСЯ hard-conflict, даже вместе с soft
    house_number mismatch (задача D: 'rooms mismatch пока оставить
    сильным конфликтом')."""
    from bot.identity.property_linker import _build_candidate_record

    listing = {"id": "L1", "address": "Абая, 10", "floor": 5, "area": 45.0, "rooms": 2,
               "seller_name": None, "price": None, "first_seen": None, "archived_at": None}
    candidate = {"id": "L2", "property_id": 1, "address": "Абая, 25", "floor": 5, "area": 45.0,
                 "rooms": 3, "seller_name": None, "price": None, "first_seen": None, "archived_at": None}
    record = _build_candidate_record(listing, candidate, "fuzzy", 0.8)
    assert record["status"] == "rejected"


# ── C. corroborating_methods ────────────────────────────────────────────

@pytest_asyncio.fixture
async def corroboration_pair(db):
    """listing A + listing B, ОДНОВРЕМЕННО exact_hash-совместимы (тот же
    адрес/этаж/площадь) И dedup_listings-помечены — задача C: обе
    независимые сигнатуры должны попасть в corroborating_methods ОДНОЙ
    строки, не потеряться."""
    from bot.db.pg import execute, fetchval
    lid_a, lid_b = "__test_pmr_corrob_a__", "__test_pmr_corrob_b__"
    await _insert_listing(lid_a, address="Коррелят, 1", floor=5, area=45.0)
    await _insert_listing(lid_b, address="Коррелят, 1", floor=5, area=45.0,
                           is_duplicate=True, duplicate_of=lid_a, dup_match="addr_price")
    from bot.identity.property_linker import compute_address_hash
    h = compute_address_hash("Коррелят, 1", 5, 45.0)
    prop_a = await fetchval(
        "INSERT INTO properties (address_hash, floor, area_sqm) VALUES ($1, 5, 45.0) RETURNING property_id", h)
    await execute(
        "INSERT INTO property_listings (property_id, listing_id, link_method, confidence) "
        "VALUES ($1, $2, 'bootstrap', 1.0)", prop_a, lid_a)
    candidate_id = await fetchval("""
        INSERT INTO property_match_candidates
            (listing_id, candidate_property_id, match_method, match_score, matcher_version, status)
        VALUES ($1, $2, 'exact_hash', 0.9, 'candidate_only_v2', 'pending') RETURNING candidate_id
    """, lid_b, prop_a)
    try:
        yield {"candidate_id": candidate_id, "listing_a": lid_b, "listing_b": lid_a, "property_id": prop_a}
    finally:
        await _cleanup(lid_a, lid_b, property_ids=[prop_a])


@pytest.mark.asyncio
async def test_recompute_corroborating_methods_finds_multiple_signals(corroboration_pair):
    from bot.identity.property_linker import recompute_corroborating_methods
    from bot.db.pg import fetchval

    found = await recompute_corroborating_methods(corroboration_pair["candidate_id"])
    assert "exact_hash" in found
    assert "dedup_listings" in found
    # Детерминированный порядок — _METHOD_ORDER, не порядок обнаружения.
    assert found.index("exact_hash") < found.index("dedup_listings")

    evidence = await fetchval(
        "SELECT evidence FROM property_match_candidates WHERE candidate_id = $1",
        corroboration_pair["candidate_id"])
    import json as _json
    ev = _json.loads(evidence) if isinstance(evidence, str) else evidence
    assert ev["corroborating_methods"] == found
    assert "corroboration_version" in ev


@pytest.mark.asyncio
async def test_recompute_corroborating_methods_idempotent(corroboration_pair):
    from bot.identity.property_linker import recompute_corroborating_methods

    r1 = await recompute_corroborating_methods(corroboration_pair["candidate_id"])
    r2 = await recompute_corroborating_methods(corroboration_pair["candidate_id"])
    assert r1 == r2


@pytest.mark.asyncio
async def test_recompute_corroborating_methods_does_not_touch_status_or_match_method(corroboration_pair):
    from bot.identity.property_linker import recompute_corroborating_methods
    from bot.db.pg import fetchrow

    before = await fetchrow(
        "SELECT status, match_method, match_score FROM property_match_candidates WHERE candidate_id=$1",
        corroboration_pair["candidate_id"])
    await recompute_corroborating_methods(corroboration_pair["candidate_id"])
    after = await fetchrow(
        "SELECT status, match_method, match_score FROM property_match_candidates WHERE candidate_id=$1",
        corroboration_pair["candidate_id"])
    assert dict(before) == dict(after)


# ── E. record_review_decision ───────────────────────────────────────────

@pytest_asyncio.fixture
async def simple_candidate(db):
    from bot.db.pg import execute, fetchval
    lid_a, lid_b = "__test_pmr_dec_a__", "__test_pmr_dec_b__"
    await _insert_listing(lid_a, address="Реш Адрес, 1", floor=5, area=45.0)
    await _insert_listing(lid_b, address="Реш Адрес, 1", floor=5, area=45.0)
    prop_b = await fetchval(
        "INSERT INTO properties (address_hash, floor, area_sqm) VALUES ('__test_pmr_dec_hash__', 5, 45.0) "
        "RETURNING property_id")
    await execute(
        "INSERT INTO property_listings (property_id, listing_id, link_method, confidence) "
        "VALUES ($1, $2, 'bootstrap', 1.0)", prop_b, lid_b)
    candidate_id = await fetchval("""
        INSERT INTO property_match_candidates
            (listing_id, candidate_property_id, match_method, match_score, matcher_version, status,
             evidence)
        VALUES ($1, $2, 'exact_hash', 0.9, 'candidate_only_v2', 'pending', '{"foo": "bar"}'::jsonb)
        RETURNING candidate_id
    """, lid_a, prop_b)
    try:
        yield {"candidate_id": candidate_id, "listing_a": lid_a, "listing_b": lid_b, "property_id": prop_b}
    finally:
        await _cleanup(lid_a, lid_b, property_ids=[prop_b])


@pytest.mark.asyncio
async def test_record_decision_accepted_updates_status_and_writes_log(simple_candidate):
    from bot.identity.review_decisions import record_review_decision
    from bot.db.pg import fetchrow

    result = await record_review_decision(simple_candidate["candidate_id"], "accepted", "pytest",
                                           comment="выглядит как одна квартира")
    assert result == {"candidate_id": simple_candidate["candidate_id"], "decision": "accepted"}

    row = await fetchrow(
        "SELECT status, reviewed_at, reviewed_by FROM property_match_candidates WHERE candidate_id=$1",
        simple_candidate["candidate_id"])
    assert row["status"] == "accepted"
    assert row["reviewed_at"] is not None
    assert row["reviewed_by"] == "pytest"

    log_row = await fetchrow(
        "SELECT decision, comment, reviewed_by, evidence_snapshot FROM property_match_review_log "
        "WHERE candidate_id=$1", simple_candidate["candidate_id"])
    assert log_row["decision"] == "accepted"
    assert log_row["comment"] == "выглядит как одна квартира"


@pytest.mark.asyncio
async def test_record_decision_rejected_updates_status(simple_candidate):
    from bot.identity.review_decisions import record_review_decision
    from bot.db.pg import fetchval

    await record_review_decision(simple_candidate["candidate_id"], "rejected", "pytest")
    status = await fetchval("SELECT status FROM property_match_candidates WHERE candidate_id=$1",
                             simple_candidate["candidate_id"])
    assert status == "rejected"


@pytest.mark.asyncio
async def test_record_decision_skip_does_not_change_status_but_logs(simple_candidate):
    from bot.identity.review_decisions import record_review_decision
    from bot.db.pg import fetchrow

    await record_review_decision(simple_candidate["candidate_id"], "skip", "pytest")
    row = await fetchrow(
        "SELECT status, reviewed_at FROM property_match_candidates WHERE candidate_id=$1",
        simple_candidate["candidate_id"])
    assert row["status"] == "pending"  # skip НЕ резолвит — снова всплывёт в очереди
    assert row["reviewed_at"] is None

    log_row = await fetchrow(
        "SELECT decision FROM property_match_review_log WHERE candidate_id=$1",
        simple_candidate["candidate_id"])
    assert log_row["decision"] == "skip"  # но решение ВСЁ РАВНО залогировано


@pytest.mark.asyncio
async def test_record_decision_never_touches_property_listings_or_properties(simple_candidate):
    """Задача, явно: "«Одна квартира» только записывает решение accepted,
    но НЕ переносит property_listings и НЕ удаляет properties" — НИКАКОГО
    физического merge в этом PR."""
    from bot.identity.review_decisions import record_review_decision
    from bot.db.pg import fetchval

    pl_before = await fetchval("SELECT count(*) FROM property_listings")
    p_before = await fetchval("SELECT count(*) FROM properties")

    await record_review_decision(simple_candidate["candidate_id"], "accepted", "pytest")

    pl_after = await fetchval("SELECT count(*) FROM property_listings")
    p_after = await fetchval("SELECT count(*) FROM properties")
    assert pl_after == pl_before
    assert p_after == p_before
    # listing_a конкретно НЕ получил property_listings строку.
    linked = await fetchval("SELECT property_id FROM property_listings WHERE listing_id=$1",
                             simple_candidate["listing_a"])
    assert linked is None


@pytest.mark.asyncio
async def test_record_decision_invalid_returns_none_for_missing_candidate(db):
    from bot.identity.review_decisions import record_review_decision
    result = await record_review_decision(-1, "accepted", "pytest")
    assert result is None


def test_record_decision_rejects_invalid_decision_value():
    import asyncio
    from bot.identity.review_decisions import record_review_decision
    with pytest.raises(ValueError):
        asyncio.run(record_review_decision(1, "maybe", "pytest"))


# ── E. очередь: приоритет сортировки ────────────────────────────────────

@pytest.mark.asyncio
async def test_queue_priority_unique_photo_and_multi_method_ranks_first(simple_candidate):
    """Задача, сортировка очереди: "1. unique photo match + другие
    сигналы" — выше всех остальных tier'ов. Проверяем priority_tier
    НАПРЯМУЮ (через get_candidate_detail, конкретный candidate_id) — НЕ
    через get_next_candidate(set()) на всю очередь: прод-таблица
    property_match_candidates уже содержит РЕАЛЬНЫЕ tier=1 кандидаты
    (после scripts/photo_evidence_scan.py canary в этой же задаче) с
    более высоким match_score, чем синтетическая фикстура — "первый в
    очереди по всей базе" здесь недетерминирован и не то, что тестируем."""
    from bot.db.pg import execute
    from bot.identity.review_decisions import get_candidate_detail

    await execute("""
        INSERT INTO property_candidate_photo_evidence
            (candidate_id, shared_unit_specific_count, model_version, processing_status)
        VALUES ($1, 2, 'test_v1', 'ok')
    """, simple_candidate["candidate_id"])
    await execute(
        "UPDATE property_match_candidates SET evidence = evidence || "
        "'{\"corroborating_methods\": [\"exact_hash\", \"photo_exact\"]}'::jsonb WHERE candidate_id = $1",
        simple_candidate["candidate_id"])

    pair = await get_candidate_detail(simple_candidate["candidate_id"])
    assert pair is not None
    assert pair["priority_tier"] == 1  # unique photo match + другой метод -> высший приоритет

    await execute("DELETE FROM property_candidate_photo_evidence WHERE candidate_id=$1",
                  simple_candidate["candidate_id"])


@pytest.mark.asyncio
async def test_queue_priority_tier_ranks_below_unique_photo_without_it(simple_candidate):
    """Тот же candidate БЕЗ photo evidence/corroboration — tier=4
    ('остальные'), ниже unique-photo tier=1 (относительное сравнение,
    независимо от прод-данных в очереди)."""
    from bot.identity.review_decisions import get_candidate_detail

    pair = await get_candidate_detail(simple_candidate["candidate_id"])
    assert pair is not None
    assert pair["priority_tier"] == 4


# ── Admin endpoint: auth + no hard merge (HTTP integration) ───────────────

@pytest_asyncio.fixture
async def admin_client(db):
    from bot.db.compat import BotDB
    from bot.admin_web import create_admin_app
    from httpx import AsyncClient, ASGITransport

    db_path = os.getenv("DB_PATH", "bot.db")
    admin_password = os.getenv("ADMIN_PASSWORD", "admin123")
    bdb = BotDB(db_path)
    await bdb.init()
    app = create_admin_app(bdb, admin_password, "test", db_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.mark.asyncio
async def test_review_page_requires_auth(admin_client):
    r = await admin_client.get("/admin/property-match-review", follow_redirects=False)
    assert r.status_code in (302, 303)
    assert "/admin/login" in r.headers.get("location", "")


@pytest.mark.asyncio
async def test_decide_endpoint_requires_auth(simple_candidate, admin_client):
    r = await admin_client.post(
        f"/admin/property-match-review/{simple_candidate['candidate_id']}/decide",
        data={"decision": "accepted"}, follow_redirects=False)
    assert r.status_code in (302, 303)
    assert "/admin/login" in r.headers.get("location", "")

    from bot.db.pg import fetchval
    status = await fetchval("SELECT status FROM property_match_candidates WHERE candidate_id=$1",
                             simple_candidate["candidate_id"])
    assert status == "pending"  # неавторизованный POST НЕ изменил решение


@pytest.mark.asyncio
async def test_review_page_renders_pair_for_authed_user(simple_candidate, admin_client):
    admin_client.cookies.set("admin_auth", "1")
    admin_client.cookies.set("admin_user", "pytest")
    # candidate_id ЯВНО (не полагаемся на get_next_candidate — прод-таблица
    # property_match_candidates содержит десятки тысяч РЕАЛЬНЫХ pending
    # строк, "следующая по очереди" почти никогда не окажется именно
    # нашей тестовой фикстурой; страница поддерживает ?candidate_id=
    # ИМЕННО для этого случая — прямой переход к конкретной паре).
    r = await admin_client.get(f"/admin/property-match-review?candidate_id={simple_candidate['candidate_id']}")
    assert r.status_code == 200
    assert "Property Match Review" in r.text
    assert simple_candidate["listing_a"] in r.text


@pytest.mark.asyncio
async def test_decide_endpoint_records_decision_and_no_physical_merge(simple_candidate, admin_client):
    admin_client.cookies.set("admin_auth", "1")
    admin_client.cookies.set("admin_user", "pytest_http")

    from bot.db.pg import fetchval
    pl_before = await fetchval("SELECT count(*) FROM property_listings")

    r = await admin_client.post(
        f"/admin/property-match-review/{simple_candidate['candidate_id']}/decide",
        data={"decision": "accepted", "comment": "http test"}, follow_redirects=False)
    assert r.status_code in (302, 303)

    status = await fetchval("SELECT status FROM property_match_candidates WHERE candidate_id=$1",
                             simple_candidate["candidate_id"])
    assert status == "accepted"
    reviewed_by = await fetchval("SELECT reviewed_by FROM property_match_candidates WHERE candidate_id=$1",
                                  simple_candidate["candidate_id"])
    assert reviewed_by == "pytest_http"

    pl_after = await fetchval("SELECT count(*) FROM property_listings")
    assert pl_after == pl_before  # НИКАКОГО физического merge через HTTP-путь тоже
