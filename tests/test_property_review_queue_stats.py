"""Регрессия для задачи 2026-08-18, "привести /admin/property-match-review
к понятной и честной очереди ручной проверки": remaining != COUNT(*),
skip -> deferred (не всплывает в основной очереди снова), auto-reject vs
manual-reject считаются отдельно, статистика считает ПАРЫ (candidate_id),
не уникальные listing'и. Тестовые строки — '__test_...__' id, удаляются в
finally (тот же паттерн, что tests/test_property_match_review.py).

Прод-таблица property_match_candidates содержит десятки тысяч реальных
строк (см. read-only audit задачи) — поэтому тесты, которым нужен ТОЧНЫЙ
remaining/queue_stats, меряют ДЕЛЬТУ (before/after вставки/решения
известного числа синтетических строк), а не абсолютное значение (тот же
принцип, что уже используют test_queue_priority_* в
tests/test_property_match_review.py — прямой candidate_id вместо
"первый в очереди по всей базе")."""
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


async def _insert_listing(lid, address=None, floor=None, area=None, rooms=None, price=None):
    import json as _json
    from bot.db.pg import execute
    await execute(
        """
        INSERT INTO apartment_listings (id, url, address, floor, area, rooms, price, photos)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
        ON CONFLICT (id) DO UPDATE SET address=$3, floor=$4, area=$5, rooms=$6, price=$7
        """,
        lid, f"https://krisha.kz/test/{lid}", address, floor, area, rooms, price, _json.dumps([]),
    )


async def _cleanup(*listing_ids, property_ids=()):
    from bot.db.pg import execute
    await execute("DELETE FROM property_match_review_log WHERE listing_id = ANY($1::text[])", list(listing_ids))
    await execute("DELETE FROM property_candidate_photo_evidence WHERE candidate_id IN "
                  "(SELECT candidate_id FROM property_match_candidates WHERE listing_id = ANY($1::text[]))",
                  list(listing_ids))
    await execute("DELETE FROM property_match_candidates WHERE listing_id = ANY($1::text[])", list(listing_ids))
    await execute("DELETE FROM property_listings WHERE listing_id = ANY($1::text[])", list(listing_ids))
    if property_ids:
        await execute("DELETE FROM properties WHERE property_id = ANY($1::int[])", list(property_ids))
    await execute("DELETE FROM apartment_listings WHERE id = ANY($1::text[])", list(listing_ids))


async def _make_property(address_hash, floor=5, area=45.0):
    from bot.db.pg import fetchval
    return await fetchval(
        "INSERT INTO properties (address_hash, floor, area_sqm) VALUES ($1, $2, $3) RETURNING property_id",
        address_hash, floor, area,
    )


async def _make_candidate(listing_id, property_id, status="pending", match_method="exact_hash",
                           relationship_type="unknown", reviewed_by=None):
    from bot.db.pg import fetchval
    reviewed_at = None if reviewed_by is None else datetime.now(timezone.utc)
    return await fetchval(
        """
        INSERT INTO property_match_candidates
            (listing_id, candidate_property_id, match_method, match_score, relationship_type,
             matcher_version, status, reviewed_by, reviewed_at)
        VALUES ($1, $2, $3, 0.9, $4, 'candidate_only_v2', $5, $6::text, $7::timestamptz)
        RETURNING candidate_id
        """,
        listing_id, property_id, match_method, relationship_type, status, reviewed_by, reviewed_at,
    )


@pytest_asyncio.fixture
async def stats_pair(db):
    """Один pending-кандидат, ничем не тронутый — базовый строительный
    блок для delta-тестов ниже."""
    lid_a, lid_b = "__test_pqs_a__", "__test_pqs_b__"
    await _insert_listing(lid_a, address="Стата, 1", floor=5, area=45.0)
    await _insert_listing(lid_b, address="Стата, 1", floor=5, area=45.0)
    prop_b = await _make_property("__test_pqs_hash__")
    from bot.db.pg import execute
    await execute(
        "INSERT INTO property_listings (property_id, listing_id, link_method, confidence) "
        "VALUES ($1, $2, 'bootstrap', 1.0)", prop_b, lid_b)
    candidate_id = await _make_candidate(lid_a, prop_b)
    try:
        yield {"candidate_id": candidate_id, "listing_a": lid_a, "listing_b": lid_b, "property_id": prop_b}
    finally:
        await _cleanup(lid_a, lid_b, property_ids=[prop_b])


# ── 1. total != remaining ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_total_is_not_reported_as_remaining(db):
    """Главный баг задачи: заголовок использовал COUNT(*) по всей таблице
    как "очередь: N", хотя показывал только pending. total (все строки,
    любой статус) НИКОГДА не должен равняться remaining, если в базе есть
    хоть один resolved (accepted/rejected/auto-rejected) кандидат.

    НЕ полагается на наполненность БД (задача 2026-08-18, разбор падения
    в CI: на чистой БД, с одной pending-фикстурой из ДРУГОГО теста,
    total==remaining==1 — корректный результат для ТОЙ БД, ложный сигнал
    для этого теста). Тест сам создаёт ровно одну pending И ровно один
    auto-rejected кандидат и проверяет ДЕЛЬТУ total/remaining до и после
    — так утверждение верно независимо от того, что уже лежит в БД
    (production с тысячами строк или пустая CI-Postgres с нуля)."""
    from bot.identity.review_decisions import queue_stats

    lid_pending = "__test_pqs_delta_pending__"
    lid_auto_rej = "__test_pqs_delta_auto_rej__"
    lid_target = "__test_pqs_delta_target__"
    props = []
    try:
        await _insert_listing(lid_pending, address="Дельта, 1", floor=5, area=45.0)
        await _insert_listing(lid_auto_rej, address="Дельта, 1", floor=5, area=45.0)
        await _insert_listing(lid_target, address="Дельта, 1", floor=5, area=45.0)
        prop = await _make_property("__test_pqs_delta_hash__")
        props = [prop]
        from bot.db.pg import execute
        await execute("INSERT INTO property_listings (property_id, listing_id, link_method, confidence) "
                      "VALUES ($1, $2, 'bootstrap', 1.0)", prop, lid_target)

        before = await queue_stats()

        await _make_candidate(lid_pending, prop, status="pending")        # остаётся в remaining
        await _make_candidate(lid_auto_rej, prop, status="rejected")      # reviewed_by=NULL -> auto, НЕ в remaining

        after = await queue_stats()

        # total вырос на 2 (обе новые строки), remaining — только на 1
        # (только pending-кандидат) — это и есть "total != remaining",
        # проверено дельтой, а не абсолютным значением.
        assert after["total"] - before["total"] == 2
        assert after["remaining"] - before["remaining"] == 1
        assert after["total"] > after["remaining"]
    finally:
        await _cleanup(lid_pending, lid_auto_rej, lid_target, property_ids=props)


# ── 2. remaining исключает accepted/rejected/deferred ───────────────────

@pytest.mark.asyncio
async def test_remaining_excludes_accepted_rejected_deferred(db):
    from bot.identity.review_decisions import remaining_count, record_review_decision

    lids = [f"__test_pqs_rem_{i}__" for i in range(5)]
    props = []
    candidates = {}
    try:
        for i, lid in enumerate(lids):
            await _insert_listing(lid, address=f"Ремайн, {i}", floor=5, area=45.0)
        # общий "second side" listing, чтобы не плодить лишние properties
        other_lid = "__test_pqs_rem_other__"
        await _insert_listing(other_lid, address="Ремайн, 99", floor=5, area=45.0)
        prop = await _make_property("__test_pqs_rem_hash__")
        from bot.db.pg import execute
        await execute(
            "INSERT INTO property_listings (property_id, listing_id, link_method, confidence) "
            "VALUES ($1, $2, 'bootstrap', 1.0)", prop, other_lid)
        props.append(prop)

        candidates["untouched"] = await _make_candidate(lids[0], prop, status="pending")
        candidates["accepted"] = await _make_candidate(lids[1], prop, status="pending")
        candidates["rejected"] = await _make_candidate(lids[2], prop, status="pending")
        candidates["deferred"] = await _make_candidate(lids[3], prop, status="pending")
        candidates["auto_rejected"] = await _make_candidate(lids[4], prop, status="rejected")  # reviewed_by=NULL

        before = await remaining_count(set())
        await record_review_decision(candidates["accepted"], "accepted", "pytest")
        await record_review_decision(candidates["rejected"], "rejected", "pytest")
        await record_review_decision(candidates["deferred"], "skip", "pytest")
        after = await remaining_count(set())

        # Только "untouched" остаётся в remaining — все остальные 4
        # выбывают (accepted/rejected -> терминальный статус; deferred ->
        # skip; auto_rejected никогда не был pending+нетронут).
        assert before - after == 3  # accepted, rejected, deferred ушли из remaining
        # untouched точно всё ещё pending и учитывается.
        from bot.db.pg import fetchval
        untouched_status = await fetchval(
            "SELECT status FROM property_match_candidates WHERE candidate_id=$1", candidates["untouched"])
        assert untouched_status == "pending"
    finally:
        await _cleanup(*lids, "__test_pqs_rem_other__", property_ids=props)


# ── 3. skip убирает пару из основной очереди ────────────────────────────

@pytest.mark.asyncio
async def test_skip_removes_pair_from_default_queue(stats_pair):
    from bot.identity.review_decisions import record_review_decision, _browse_where
    from bot.db.pg import fetchval

    async def _matches_default_browse(candidate_id):
        where, params = _browse_where(set())
        sql = (
            "SELECT count(*) FROM property_match_candidates pmc "
            "LEFT JOIN property_candidate_photo_evidence pcpe ON pcpe.candidate_id = pmc.candidate_id "
            f"WHERE {where} AND pmc.candidate_id = ${len(params) + 1}"
        )
        return await fetchval(sql, *params, candidate_id) == 1

    cid = stats_pair["candidate_id"]
    assert await _matches_default_browse(cid)  # untouched pending -> в основной очереди

    await record_review_decision(cid, "skip", "pytest")
    assert not await _matches_default_browse(cid)  # после skip -> исчезает из основной очереди


# ── 4. deferred доступен через отдельный фильтр ─────────────────────────

@pytest.mark.asyncio
async def test_deferred_accessible_via_dedicated_filter(stats_pair):
    from bot.identity.review_decisions import record_review_decision, _where_for_filters
    from bot.db.pg import fetchval

    cid = stats_pair["candidate_id"]
    await record_review_decision(cid, "skip", "pytest")

    where, params = _where_for_filters({"deferred"})
    matched = await fetchval(
        f"SELECT count(*) FROM property_match_candidates pmc "
        f"LEFT JOIN property_candidate_photo_evidence pcpe ON pcpe.candidate_id = pmc.candidate_id "
        f"WHERE {where} AND pmc.candidate_id = ${len(params) + 1}",
        *params, cid,
    )
    assert matched == 1

    # Пару можно открыть и решить окончательно (get_candidate_detail
    # работает независимо от фильтров, задача, явно: "можно открыть и
    # принять окончательное решение позже").
    from bot.identity.review_decisions import get_candidate_detail
    pair = await get_candidate_detail(cid)
    assert pair is not None
    assert pair["is_deferred"] is True

    await record_review_decision(cid, "accepted", "pytest")
    pair_after = await get_candidate_detail(cid)
    assert pair_after["status"] == "accepted"
    assert pair_after["is_deferred"] is False  # решено -> больше не "отложено"


# ── 5. повторный skip идемпотентен ──────────────────────────────────────

@pytest.mark.asyncio
async def test_repeated_skip_is_idempotent(stats_pair):
    from bot.identity.review_decisions import record_review_decision, get_candidate_detail
    from bot.db.pg import fetchval

    cid = stats_pair["candidate_id"]
    await record_review_decision(cid, "skip", "pytest")
    pair_1 = await get_candidate_detail(cid)
    await record_review_decision(cid, "skip", "pytest")
    pair_2 = await get_candidate_detail(cid)

    # Наблюдаемое состояние очереди не меняется от повторного skip.
    assert pair_1["is_deferred"] is True
    assert pair_2["is_deferred"] is True
    assert pair_1["status"] == pair_2["status"] == "pending"

    # Журнал решений при этом append-only — растёт (НЕ перезаписывается),
    # задача, явно: "история всех решений остаётся append-only".
    log_count = await fetchval(
        "SELECT count(*) FROM property_match_review_log WHERE candidate_id=$1", cid)
    assert log_count == 2


# ── 6. статистика считает пары, а не уникальные listings ────────────────

@pytest.mark.asyncio
async def test_stats_count_pairs_not_unique_listings(db):
    """Один listing_id, участвующий в ДВУХ разных candidate-парах (две
    разные candidate_property_id) -> remaining растёт на 2, не на 1."""
    from bot.identity.review_decisions import remaining_count

    shared_lid = "__test_pqs_shared__"
    other_a, other_b = "__test_pqs_other_a__", "__test_pqs_other_b__"
    props = []
    try:
        await _insert_listing(shared_lid, address="Шаред, 1", floor=5, area=45.0)
        await _insert_listing(other_a, address="Шаред, 1", floor=5, area=45.0)
        await _insert_listing(other_b, address="Шаред, 1", floor=6, area=45.0)
        prop_a = await _make_property("__test_pqs_shared_hash_a__")
        prop_b = await _make_property("__test_pqs_shared_hash_b__", floor=6)
        props = [prop_a, prop_b]
        from bot.db.pg import execute
        await execute("INSERT INTO property_listings (property_id, listing_id, link_method, confidence) "
                      "VALUES ($1, $2, 'bootstrap', 1.0)", prop_a, other_a)
        await execute("INSERT INTO property_listings (property_id, listing_id, link_method, confidence) "
                      "VALUES ($1, $2, 'bootstrap', 1.0)", prop_b, other_b)

        before = await remaining_count(set())
        await _make_candidate(shared_lid, prop_a, status="pending")  # пара 1: shared_lid<->other_a
        await _make_candidate(shared_lid, prop_b, status="pending")  # пара 2: shared_lid<->other_b (ТОТ ЖЕ listing)
        after = await remaining_count(set())

        # Один и тот же listing_id, две пары -> +2 к remaining, НЕ +1
        # (задача, явно: "Одно объявление может участвовать в нескольких
        # парах" — счётчик не должен схлопывать их до уникальных listing'ов).
        assert after - before == 2
    finally:
        await _cleanup(shared_lid, other_a, other_b, property_ids=props)


# ── 7. filtered remaining корректен ─────────────────────────────────────

@pytest.mark.asyncio
async def test_filtered_remaining_is_correct(db):
    from bot.identity.review_decisions import remaining_count

    lid_concurrent = "__test_pqs_filt_conc__"
    lid_other = "__test_pqs_filt_other__"
    lid_target = "__test_pqs_filt_target__"
    props = []
    try:
        await _insert_listing(lid_concurrent, address="Фильтр, 1", floor=5, area=45.0)
        await _insert_listing(lid_other, address="Фильтр, 1", floor=5, area=45.0)
        await _insert_listing(lid_target, address="Фильтр, 1", floor=5, area=45.0)
        prop = await _make_property("__test_pqs_filt_hash__")
        props = [prop]
        from bot.db.pg import execute
        await execute("INSERT INTO property_listings (property_id, listing_id, link_method, confidence) "
                      "VALUES ($1, $2, 'bootstrap', 1.0)", prop, lid_target)

        before_all = await remaining_count(set())
        before_concurrent = await remaining_count({"concurrent"})

        await _make_candidate(lid_concurrent, prop, status="pending", relationship_type="concurrent_duplicate")
        await _make_candidate(lid_other, prop, status="pending", relationship_type="unknown")

        after_all = await remaining_count(set())
        after_concurrent = await remaining_count({"concurrent"})

        assert after_all - before_all == 2         # обе новые пары в общей очереди
        assert after_concurrent - before_concurrent == 1  # только concurrent-пара под фильтром
    finally:
        await _cleanup(lid_concurrent, lid_other, lid_target, property_ids=props)


# ── 8. progress bar корректен при пустой и заполненной очереди ──────────

def test_progress_pct_empty_queue_is_zero_not_error():
    from bot.identity.review_decisions import _progress_pct
    assert _progress_pct(0, 0) == 0  # eligible_total=0 -> 0%, не ZeroDivisionError


def test_progress_pct_full_queue():
    from bot.identity.review_decisions import _progress_pct
    assert _progress_pct(10, 10) == 100
    assert _progress_pct(5, 10) == 50
    assert _progress_pct(0, 10) == 0
    assert _progress_pct(1, 3) == 33  # округление, не floor/ceil-специфика


# ── 9. auto-rejected и manual rejected считаются отдельно ───────────────

@pytest.mark.asyncio
async def test_auto_rejected_and_manual_rejected_counted_separately(db):
    from bot.identity.review_decisions import queue_stats, record_review_decision

    lid_auto = "__test_pqs_auto_rej__"
    lid_manual = "__test_pqs_manual_rej__"
    lid_target = "__test_pqs_rej_target__"
    props = []
    try:
        await _insert_listing(lid_auto, address="Реджект, 1", floor=5, area=45.0)
        await _insert_listing(lid_manual, address="Реджект, 1", floor=5, area=45.0)
        await _insert_listing(lid_target, address="Реджект, 1", floor=5, area=45.0)
        prop = await _make_property("__test_pqs_rej_hash__")
        props = [prop]
        from bot.db.pg import execute
        await execute("INSERT INTO property_listings (property_id, listing_id, link_method, confidence) "
                      "VALUES ($1, $2, 'bootstrap', 1.0)", prop, lid_target)

        before = await queue_stats()

        # auto-reject: INSERT напрямую со status='rejected', reviewed_by
        # остаётся NULL — ровно то, что делает property_linker.py при
        # hard conflict (rooms mismatch), НЕ через record_review_decision.
        await _make_candidate(lid_auto, prop, status="rejected")

        # manual reject: через record_review_decision — reviewed_by
        # проставляется, задача явно требует различать эти два случая.
        manual_cid = await _make_candidate(lid_manual, prop, status="pending")
        await record_review_decision(manual_cid, "rejected", "pytest")

        after = await queue_stats()

        assert after["rejected_auto"] - before["rejected_auto"] == 1
        assert after["rejected_manual"] - before["rejected_manual"] == 1
        # "проверено" (reviewed) считает только ручные решения, НЕ auto-reject.
        assert after["reviewed"] - before["reviewed"] == 1
    finally:
        await _cleanup(lid_auto, lid_manual, lid_target, property_ids=props)


# ── 10. решения не выполняют physical merge (skip тоже) ─────────────────

@pytest.mark.asyncio
async def test_skip_decision_does_not_touch_property_listings_or_properties(stats_pair):
    """test_property_match_review.py уже проверяет это для accepted —
    здесь то же самое для skip/deferred (задача, явно: "решения не
    выполняют physical merge", без исключений по типу решения)."""
    from bot.identity.review_decisions import record_review_decision
    from bot.db.pg import fetchval

    pl_before = await fetchval("SELECT count(*) FROM property_listings")
    p_before = await fetchval("SELECT count(*) FROM properties")

    await record_review_decision(stats_pair["candidate_id"], "skip", "pytest")

    pl_after = await fetchval("SELECT count(*) FROM property_listings")
    p_after = await fetchval("SELECT count(*) FROM properties")
    assert pl_after == pl_before
    assert p_after == p_before
