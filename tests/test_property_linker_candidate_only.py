"""Регрессия для задачи 2026-08-16 ("безопасная инфраструктура
кандидатов" — миграция 086, match_mode="candidate_only") —
bot/identity/property_linker.py. НОВЫЙ ДЕФОЛТ: listing → ВСЕГДА своя
provisional property, ни один сигнал (exact_hash/fuzzy/dedup_listings)
не создаёт hard link — все становятся property_match_candidates.

Мид-ту-уточнение (та же задача): simultaneous activity НЕ безусловный
конфликт — одна квартира может продаваться через 5 агентов одновременно
(concurrent_duplicate), relationship_type хранится ОТДЕЛЬНО от status."""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import pytest
import pytest_asyncio
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")
NOW = datetime(2026, 8, 16, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def db():
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    yield
    await close_pool()


async def _insert_listing(lid, address=None, floor=None, area=None, rooms=None, complex_name=None,
                           first_seen=None, last_seen=None, archived_at=None, price=None,
                           seller_name=None, is_duplicate=False, duplicate_of=None, dup_match=None):
    from bot.db.pg import execute
    await execute(
        """
        INSERT INTO apartment_listings (id, url, address, floor, area, rooms, complex_name,
                                         first_seen, last_seen, archived_at, price, seller_name,
                                         is_duplicate, duplicate_of, dup_match)
        VALUES ($1, $2, $3, $4, $5, $6, $7, COALESCE($8, now()), COALESCE($9, now()), $10, $11, $12, $13, $14, $15)
        ON CONFLICT (id) DO UPDATE SET address=$3, floor=$4, area=$5, rooms=$6, complex_name=$7,
                                        first_seen=COALESCE($8, apartment_listings.first_seen),
                                        last_seen=COALESCE($9, apartment_listings.last_seen),
                                        archived_at=$10, price=$11, seller_name=$12,
                                        is_duplicate=$13, duplicate_of=$14, dup_match=$15
        """,
        lid, f"https://krisha.kz/test/{lid}", address, floor, area, rooms, complex_name,
        first_seen, last_seen, archived_at, price, seller_name, is_duplicate, duplicate_of, dup_match,
    )


async def _insert_complex(name):
    from bot.db.pg import fetchval
    return await fetchval("INSERT INTO complexes (name, is_garbage) VALUES ($1, FALSE) RETURNING id", name)


async def _cleanup(*listing_ids, complex_ids=(), address_hashes=()):
    from bot.db.pg import execute
    await execute("DELETE FROM property_match_candidates WHERE listing_id = ANY($1::text[])", list(listing_ids))
    await execute("DELETE FROM property_listings WHERE listing_id = ANY($1::text[])", list(listing_ids))
    if address_hashes:
        await execute("DELETE FROM properties WHERE address_hash = ANY($1::text[])", list(address_hashes))
    await execute("DELETE FROM apartment_listings WHERE id = ANY($1::text[])", list(listing_ids))
    if complex_ids:
        await execute("DELETE FROM complexes WHERE id = ANY($1::int[])", list(complex_ids))


# ── 1. Одинаковый address_hash создаёт ДВЕ provisional properties ───────

@pytest.mark.asyncio
async def test_same_address_hash_creates_two_provisional_properties(db):
    from bot.identity.property_linker import link_listing_to_property, compute_address_hash

    lid_a, lid_b = "__test_co_same_a__", "__test_co_same_b__"
    await _insert_listing(lid_a, address="Тест Адрес, 1", floor=5, area=45.0, rooms=2)
    await _insert_listing(lid_b, address="Тест Адрес, 1", floor=5, area=45.0, rooms=2)
    h = compute_address_hash("Тест Адрес, 1", 5, 45.0)
    try:
        r1 = await link_listing_to_property(
            {"id": lid_a, "address": "Тест Адрес, 1", "floor": 5, "area": 45.0, "rooms": 2, "complex_name": None})
        r2 = await link_listing_to_property(
            {"id": lid_b, "address": "Тест Адрес, 1", "floor": 5, "area": 45.0, "rooms": 2, "complex_name": None})
        assert r1["method"] == r2["method"] == "bootstrap"
        assert r1["property_id"] != r2["property_id"]  # ДВЕ отдельные property, не hard link
        assert r1["match_mode"] == "candidate_only"
    finally:
        await _cleanup(lid_a, lid_b, address_hashes=[h])


# ── 2. Exact совпадение создаёт candidate ─────────────────────────────────

@pytest.mark.asyncio
async def test_exact_match_creates_candidate_not_hard_link(db):
    from bot.db.pg import fetchval
    from bot.identity.property_linker import link_listing_to_property, compute_address_hash

    lid_a, lid_b = "__test_co_exact_a__", "__test_co_exact_b__"
    await _insert_listing(lid_a, address="Тест Адрес, 2", floor=5, area=45.0, rooms=2)
    await _insert_listing(lid_b, address="Тест Адрес, 2", floor=5, area=45.0, rooms=2)
    h = compute_address_hash("Тест Адрес, 2", 5, 45.0)
    try:
        r1 = await link_listing_to_property(
            {"id": lid_a, "address": "Тест Адрес, 2", "floor": 5, "area": 45.0, "rooms": 2, "complex_name": None})
        r2 = await link_listing_to_property(
            {"id": lid_b, "address": "Тест Адрес, 2", "floor": 5, "area": 45.0, "rooms": 2, "complex_name": None})
        assert len(r2["candidates"]) == 1
        c = r2["candidates"][0]
        assert c["match_method"] == "exact_hash"
        assert c["candidate_property_id"] == r1["property_id"]
        assert c["match_score"] < 1.0  # НЕ 1.0 просто за совпадение хэша

        stored = await fetchval(
            "SELECT count(*) FROM property_match_candidates WHERE listing_id=$1 AND match_method='exact_hash'", lid_b)
        assert stored == 1
    finally:
        await _cleanup(lid_a, lid_b, address_hashes=[h])


# ── 3. Fuzzy создаёт candidate ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fuzzy_match_creates_candidate(db):
    from bot.identity.property_linker import link_listing_to_property, compute_address_hash

    cid = await _insert_complex("__test_co_fuzzy_complex__")
    lid_a, lid_b = "__test_co_fuzzy_a__", "__test_co_fuzzy_b__"
    await _insert_listing(lid_a, address="Адрес А, 3", floor=7, area=60.0, rooms=2,
                           complex_name="__test_co_fuzzy_complex__")
    await _insert_listing(lid_b, address="Адрес Б, 4", floor=7, area=60.8, rooms=2,
                           complex_name="__test_co_fuzzy_complex__")
    h1 = compute_address_hash("Адрес А, 3", 7, 60.0)
    h2 = compute_address_hash("Адрес Б, 4", 7, 60.8)
    try:
        r1 = await link_listing_to_property(
            {"id": lid_a, "address": "Адрес А, 3", "floor": 7, "area": 60.0, "rooms": 2,
             "complex_name": "__test_co_fuzzy_complex__"})
        r2 = await link_listing_to_property(
            {"id": lid_b, "address": "Адрес Б, 4", "floor": 7, "area": 60.8, "rooms": 2,
             "complex_name": "__test_co_fuzzy_complex__"})
        assert r1["property_id"] != r2["property_id"]  # НЕ hard link
        fuzzy_candidates = [c for c in r2["candidates"] if c["match_method"] == "fuzzy"]
        assert len(fuzzy_candidates) == 1
        assert fuzzy_candidates[0]["candidate_property_id"] == r1["property_id"]
    finally:
        await _cleanup(lid_a, lid_b, complex_ids=[cid], address_hashes=[h1, h2])


# ── 4. Rooms mismatch фиксируется как conflict ───────────────────────────

@pytest.mark.asyncio
async def test_rooms_mismatch_recorded_as_conflict_and_auto_rejected(db):
    from bot.identity.property_linker import link_listing_to_property, compute_address_hash

    lid_a, lid_b = "__test_co_rooms_a__", "__test_co_rooms_b__"
    await _insert_listing(lid_a, address="Тест Адрес, 5", floor=5, area=45.0, rooms=2)
    await _insert_listing(lid_b, address="Тест Адрес, 5", floor=5, area=45.0, rooms=3)  # разное число комнат
    h = compute_address_hash("Тест Адрес, 5", 5, 45.0)
    try:
        r1 = await link_listing_to_property(
            {"id": lid_a, "address": "Тест Адрес, 5", "floor": 5, "area": 45.0, "rooms": 2, "complex_name": None})
        r2 = await link_listing_to_property(
            {"id": lid_b, "address": "Тест Адрес, 5", "floor": 5, "area": 45.0, "rooms": 3, "complex_name": None})
        c = r2["candidates"][0]
        assert c["conflict_reasons"] is not None
        assert any("rooms" in reason for reason in c["conflict_reasons"])
        assert c["status"] == "rejected"
        # НЕ hard link при этом — обе properties остаются отдельными.
        assert r1["property_id"] != r2["property_id"]
    finally:
        await _cleanup(lid_a, lid_b, address_hashes=[h])


# ── 5. dedup signal НЕ делает auto-merge ──────────────────────────────────

@pytest.mark.asyncio
async def test_dedup_listings_signal_creates_candidate_not_automerge(db):
    """bot/core/dedup_listings.py — независимый evidence (задача:
    "но не автоматическое подтверждение")."""
    from bot.identity.property_linker import link_listing_to_property, compute_address_hash

    lid_a, lid_b = "__test_co_dedup_a__", "__test_co_dedup_b__"
    await _insert_listing(lid_a, address="Дедуп Адрес, 6", floor=5, area=45.0)
    await _insert_listing(lid_b, address="Совсем Другой Адрес, 99", floor=5, area=45.0,
                           is_duplicate=True, duplicate_of=lid_a, dup_match="addr_price")
    h1 = compute_address_hash("Дедуп Адрес, 6", 5, 45.0)
    h2 = compute_address_hash("Совсем Другой Адрес, 99", 5, 45.0)
    try:
        r1 = await link_listing_to_property(
            {"id": lid_a, "address": "Дедуп Адрес, 6", "floor": 5, "area": 45.0, "rooms": None, "complex_name": None})
        r2 = await link_listing_to_property(
            {"id": lid_b, "address": "Совсем Другой Адрес, 99", "floor": 5, "area": 45.0, "rooms": None,
             "complex_name": None, "duplicate_of": lid_a, "is_duplicate": True, "dup_match": "addr_price"})
        assert r1["property_id"] != r2["property_id"]  # НЕ auto-merge

        dedup_candidates = [c for c in r2["candidates"] if c["match_method"] == "dedup_listings"]
        assert len(dedup_candidates) == 1
        assert dedup_candidates[0]["candidate_property_id"] == r1["property_id"]
        assert dedup_candidates[0]["status"] in ("pending", "rejected")  # НЕ 'accepted' автоматически
    finally:
        await _cleanup(lid_a, lid_b, address_hashes=[h1, h2])


@pytest.mark.asyncio
async def test_dedup_candidate_found_in_dry_run_via_bootstrap_index(db):
    """РЕГРЕССИЯ на реальный баг (найден на прогоне --dry-run по ВСЕЙ
    базе, 50352 строк — dedup_candidates=0, хотя 29.5% listing'ов
    помечены is_duplicate=TRUE): _find_dedup_candidate_property искал
    "другой" listing ТОЛЬКО через property_listings (реальную БД,
    никогда не меняющуюся за dry-run) — тот же класс бага, что уже был
    у exact_hash/fuzzy. Фикс — BootstrapIndex.find_listing()."""
    from backfill_property_ids import run_backfill
    from bot.identity.property_linker import compute_address_hash

    lid_a, lid_b = "__test_co_dedup_dry_a__", "__test_co_dedup_dry_b__"
    await _insert_listing(lid_a, address="Дедуп Драй Адрес, 14", floor=5, area=45.0)
    await _insert_listing(lid_b, address="Совсем Другой Драй Адрес, 100", floor=5, area=45.0,
                           is_duplicate=True, duplicate_of=lid_a, dup_match="addr_price")
    h1 = compute_address_hash("Дедуп Драй Адрес, 14", 5, 45.0)
    h2 = compute_address_hash("Совсем Другой Драй Адрес, 100", 5, 45.0)
    try:
        # id ASC гарантирует lid_a обработан раньше lid_b (алфавитный
        # порядок совпадает с порядком вставки здесь).
        stats = await run_backfill(dry_run=True, listing_ids=[lid_a, lid_b],
                                    match_mode="candidate_only", order_by="id")
        assert stats["dedup_candidates"] >= 1
    finally:
        await _cleanup(lid_a, lid_b, address_hashes=[h1, h2])


# ── 6. Повторный запуск идемпотентен ──────────────────────────────────────

@pytest.mark.asyncio
async def test_repeated_run_is_idempotent(db):
    from backfill_property_ids import run_backfill
    from bot.db.pg import fetchval

    lids = ["__test_co_idem_a__", "__test_co_idem_b__"]
    await _insert_listing(lids[0], address="Идем Адрес, 7", floor=3, area=40.0)
    await _insert_listing(lids[1], address="Идем Адрес, 7", floor=3, area=40.0)
    from bot.identity.property_linker import compute_address_hash
    h = compute_address_hash("Идем Адрес, 7", 3, 40.0)
    try:
        first = await run_backfill(listing_ids=lids, match_mode="candidate_only")
        second = await run_backfill(listing_ids=lids, match_mode="candidate_only")

        assert first["provisional_created"] == 2
        assert second["already_linked"] == 2
        assert second["provisional_created"] == 0
        assert first["properties_total_after"] == second["properties_total_after"]

        candidate_count = await fetchval(
            "SELECT count(*) FROM property_match_candidates WHERE listing_id = ANY($1::text[])", lids)
        assert candidate_count == 1  # только одна пара, не дублирована повторным прогоном
    finally:
        await _cleanup(*lids, address_hashes=[h])


# ── 7. Порядок обработки не влияет ────────────────────────────────────────

@pytest.mark.asyncio
async def test_order_independence_via_dry_run(db):
    from backfill_property_ids import run_backfill
    from bot.identity.property_linker import compute_address_hash

    cid = await _insert_complex("__test_co_order_complex__")
    lids = [f"__test_co_order_{i}__" for i in range(4)]
    specs = [
        ("Заказ А, 8", 4, 50.0), ("Заказ А, 8", 4, 50.0),  # exact relist пара
        ("Заказ Б, 9", 4, 50.5),  # fuzzy-адъюнкт
        ("Заказ В, 10", 9, 70.0),
    ]
    hashes = []
    try:
        for lid, (addr, floor, area) in zip(lids, specs):
            await _insert_listing(lid, address=addr, floor=floor, area=area, complex_name="__test_co_order_complex__")
            hashes.append(compute_address_hash(addr, floor, area))

        results = {}
        for order in ("id", "id_desc", "listed_at", "shuffled_42"):
            results[order] = await run_backfill(
                dry_run=True, listing_ids=lids, match_mode="candidate_only", order_by=order)

        reference = results["id"]
        for order, stats in results.items():
            # provisional_created — ВСЕГДА total (bootstrap не зависит от
            # того, что уже существует) — единственная величина, которая
            # СТРУКТУРНО гарантированно order-independent для candidate_only.
            assert stats["provisional_created"] == reference["provisional_created"] == 4
            assert stats["skipped"] == reference["skipped"] == 0
            # РЕГРЕССИЯ на реальный баг (найден на прогоне --dry-run по
            # ВСЕЙ базе, 50352 строк — 0 exact/fuzzy кандидатов вместо
            # тысяч, ожидаемых из предыдущих read-only аудитов):
            # BootstrapIndex-кандидаты (виртуальные, в рамках ЭТОГО
            # dry-run прохода) молча выбрасывались "if pid is not None"
            # в _generate_candidates. Пара "Заказ А, 8" (два listing,
            # тот же address_hash) ДОЛЖНА дать хотя бы 1 exact-candidate
            # в ЛЮБОМ порядке, а не только когда порядок совпал с id ASC.
            assert stats["exact_candidates"] >= 1, \
                f"order={order}: dry-run НЕ нашёл exact-кандидата виртуальной пары в том же проходе"
    finally:
        await _cleanup(*lids, complex_ids=[cid], address_hashes=hashes)


# ── 8. accepted/rejected candidate не дублируется ────────────────────────

@pytest.mark.asyncio
async def test_candidate_not_duplicated_on_rerun(db):
    from bot.db.pg import fetchval, execute
    from bot.identity.property_linker import link_listing_to_property, compute_address_hash

    lid_a, lid_b = "__test_co_nodup_a__", "__test_co_nodup_b__"
    await _insert_listing(lid_a, address="Тест Адрес, 11", floor=5, area=45.0)
    await _insert_listing(lid_b, address="Тест Адрес, 11", floor=5, area=45.0)
    h = compute_address_hash("Тест Адрес, 11", 5, 45.0)
    try:
        r1 = await link_listing_to_property(
            {"id": lid_a, "address": "Тест Адрес, 11", "floor": 5, "area": 45.0, "rooms": None, "complex_name": None})
        r2 = await link_listing_to_property(
            {"id": lid_b, "address": "Тест Адрес, 11", "floor": 5, "area": 45.0, "rooms": None, "complex_name": None})
        candidate_id = r2["candidates"][0]["candidate_property_id"]

        # Человек принял решение (симулируем review) — статус меняется руками.
        await execute(
            "UPDATE property_match_candidates SET status='accepted', reviewed_at=now(), reviewed_by='test' "
            "WHERE listing_id=$1 AND candidate_property_id=$2", lid_b, candidate_id)

        # already_linked для ОБОИХ listing'ов теперь короткое замыкание —
        # повторный вызов link_listing_to_property не пересчитывает и не
        # дублирует кандидата (тот же принцип идемпотентности, что и для
        # property_listings).
        r2_again = await link_listing_to_property(
            {"id": lid_b, "address": "Тест Адрес, 11", "floor": 5, "area": 45.0, "rooms": None, "complex_name": None})
        assert r2_again["method"] == "already_linked"

        count = await fetchval(
            "SELECT count(*) FROM property_match_candidates WHERE listing_id=$1 AND candidate_property_id=$2",
            lid_b, candidate_id)
        assert count == 1  # не задвоился
        status = await fetchval(
            "SELECT status FROM property_match_candidates WHERE listing_id=$1 AND candidate_property_id=$2",
            lid_b, candidate_id)
        assert status == "accepted"  # решение человека не затёрто повторным прогоном
    finally:
        await _cleanup(lid_a, lid_b, address_hashes=[h])


# ── 9. Legacy unsafe mode НЕ дефолт ───────────────────────────────────────

@pytest.mark.asyncio
async def test_default_match_mode_is_candidate_only(db):
    from bot.identity.property_linker import link_listing_to_property, compute_address_hash

    lid = "__test_co_default__"
    await _insert_listing(lid, address="Дефолт Адрес, 12", floor=1, area=30.0)
    h = compute_address_hash("Дефолт Адрес, 12", 1, 30.0)
    try:
        r = await link_listing_to_property(
            {"id": lid, "address": "Дефолт Адрес, 12", "floor": 1, "area": 30.0, "rooms": None, "complex_name": None})
        assert r["match_mode"] == "candidate_only"
        assert r["method"] == "bootstrap"  # НЕ 'auto'/'fuzzy' (legacy hard-link методы)
    finally:
        await _cleanup(lid, address_hashes=[h])


def test_backfill_cli_default_is_candidate_only():
    import backfill_property_ids as mod
    import inspect

    sig = inspect.signature(mod.run_backfill)
    assert sig.parameters["match_mode"].default == "candidate_only"


def test_legacy_modes_require_explicit_opt_in():
    """exact_only/fuzzy остаются валидными, но НЕ дефолтом — задача:
    "Legacy exact_only/fuzzy оставить только для исследований, явно
    пометить unsafe и запретить как default backfill"."""
    from bot.identity.property_linker import _VALID_MATCH_MODES
    import inspect
    from bot.identity.property_linker import link_listing_to_property

    sig = inspect.signature(link_listing_to_property)
    assert sig.parameters["match_mode"].default == "candidate_only"
    assert {"candidate_only", "exact_only", "fuzzy"} == set(_VALID_MATCH_MODES)


# ── Мид-ту-уточнение: simultaneous activity НЕ безусловный конфликт ─────

@pytest.mark.asyncio
async def test_simultaneous_activity_does_not_auto_reject(db):
    """Задача, мид-ту: "Одна физическая квартира может одновременно
    продаваться через 5 агентов с разными listing_id... это НЕ
    безусловный конфликт" — пересекающиеся интервалы активности САМИ ПО
    СЕБЕ не должны попадать в conflict_reasons/провоцировать status=
    'rejected'."""
    from bot.identity.property_linker import link_listing_to_property, compute_address_hash

    overlap_start = NOW - timedelta(days=10)
    lid_a, lid_b = "__test_co_simul_a__", "__test_co_simul_b__"
    await _insert_listing(lid_a, address="Тест Адрес, 13", floor=5, area=45.0, rooms=2,
                           seller_name="Агент А", first_seen=overlap_start, archived_at=None)
    await _insert_listing(lid_b, address="Тест Адрес, 13", floor=5, area=45.0, rooms=2,
                           seller_name="Агент Б", first_seen=overlap_start, archived_at=None)
    h = compute_address_hash("Тест Адрес, 13", 5, 45.0)
    try:
        r1 = await link_listing_to_property(
            {"id": lid_a, "address": "Тест Адрес, 13", "floor": 5, "area": 45.0, "rooms": 2,
             "complex_name": None, "seller_name": "Агент А", "first_seen": overlap_start, "archived_at": None})
        r2 = await link_listing_to_property(
            {"id": lid_b, "address": "Тест Адрес, 13", "floor": 5, "area": 45.0, "rooms": 2,
             "complex_name": None, "seller_name": "Агент Б", "first_seen": overlap_start, "archived_at": None})
        c = r2["candidates"][0]
        assert c["evidence"]["simultaneously_active"] is True
        # НЕ конфликт — rooms/house_number совпадают, только активность
        # пересекается (разные агенты).
        assert c["conflict_reasons"] is None
        assert c["status"] == "pending"  # НЕ auto-rejected
    finally:
        await _cleanup(lid_a, lid_b, address_hashes=[h])


@pytest.mark.asyncio
async def test_concurrent_duplicate_relationship_type_with_matching_house_number(db):
    """Пересечение активности + совпадающий номер дома (сильный сигнал)
    -> relationship_type='concurrent_duplicate' — та же квартира через
    несколько агентов, НЕ 'разные квартиры'."""
    from bot.identity.property_linker import link_listing_to_property, compute_address_hash

    # Одинаковый address+floor+area -> exact_hash кандидат (НЕ fuzzy —
    # fuzzy требует complex_id, здесь сознательно его нет, exact_hash
    # candidate-поиск в нём не нуждается).
    overlap_start = NOW - timedelta(days=5)
    lid_a, lid_b = "__test_co_concur_a__", "__test_co_concur_b__"
    await _insert_listing(lid_a, address="Кабанбай батыра, 15", floor=5, area=45.0,
                           seller_name="Хозяин", first_seen=overlap_start, archived_at=None)
    await _insert_listing(lid_b, address="Кабанбай батыра, 15", floor=5, area=45.0,
                           seller_name="Риелтор Игорь", first_seen=overlap_start, archived_at=None)
    h = compute_address_hash("Кабанбай батыра, 15", 5, 45.0)
    try:
        r1 = await link_listing_to_property(
            {"id": lid_a, "address": "Кабанбай батыра, 15", "floor": 5, "area": 45.0, "rooms": None,
             "complex_name": None, "seller_name": "Хозяин", "first_seen": overlap_start, "archived_at": None})
        r2 = await link_listing_to_property(
            {"id": lid_b, "address": "Кабанбай батыра, 15", "floor": 5, "area": 45.0, "rooms": None,
             "complex_name": None, "seller_name": "Риелтор Игорь", "first_seen": overlap_start, "archived_at": None})
        assert len(r2["candidates"]) >= 1
        c = r2["candidates"][0]
        assert c["relationship_type"] == "concurrent_duplicate"
        assert c["status"] != "rejected"  # НЕ отклонён из-за одновременной активности
    finally:
        await _cleanup(lid_a, lid_b, address_hashes=[h])


@pytest.mark.asyncio
async def test_sequential_no_overlap_with_evidence_is_relist_relationship(db):
    """НЕ пересекались по времени + совпадающие признаки ->
    relationship_type='relist'."""
    from bot.identity.property_linker import link_listing_to_property, compute_address_hash

    lid_a, lid_b = "__test_co_relist_a__", "__test_co_relist_b__"
    await _insert_listing(lid_a, address="Момышулы, 20", floor=5, area=45.0, seller_name="Продавец С",
                           first_seen=NOW - timedelta(days=60), archived_at=NOW - timedelta(days=40))
    await _insert_listing(lid_b, address="Момышулы, 20", floor=5, area=45.0, seller_name="Продавец С",
                           first_seen=NOW - timedelta(days=20), archived_at=None)
    h = compute_address_hash("Момышулы, 20", 5, 45.0)
    try:
        r1 = await link_listing_to_property(
            {"id": lid_a, "address": "Момышулы, 20", "floor": 5, "area": 45.0, "rooms": None, "complex_name": None,
             "seller_name": "Продавец С", "first_seen": NOW - timedelta(days=60), "archived_at": NOW - timedelta(days=40)})
        r2 = await link_listing_to_property(
            {"id": lid_b, "address": "Момышулы, 20", "floor": 5, "area": 45.0, "rooms": None, "complex_name": None,
             "seller_name": "Продавец С", "first_seen": NOW - timedelta(days=20), "archived_at": None})
        c = r2["candidates"][0]
        assert c["evidence"]["simultaneously_active"] is False
        assert c["relationship_type"] == "relist"
    finally:
        await _cleanup(lid_a, lid_b, address_hashes=[h])


def test_evidence_schema_ready_for_future_photo_matching():
    """Задача, мид-ту: "perceptual image matching пока можно оставить
    следующим PR, но схема evidence должна его поддерживать" — ключи
    зарезервированы уже сейчас, даже если метод пока not_implemented."""
    from bot.identity.property_linker import _build_candidate_record

    listing = {"id": "L1", "address": "Адрес, 1", "floor": 5, "area": 45.0, "rooms": 2,
               "seller_name": None, "price": None, "first_seen": None, "archived_at": None}
    candidate = {"id": "L2", "property_id": 1, "address": "Адрес, 1", "floor": 5, "area": 45.0,
                 "rooms": 2, "seller_name": None, "price": None, "first_seen": None, "archived_at": None}
    record = _build_candidate_record(listing, candidate, "exact_hash", 0.9)
    assert "photo_signal" in record["evidence"]
    assert record["evidence"]["photo_signal"]["method"] == "not_implemented"
    assert "shared_rare_photo_count" in record["evidence"]["photo_signal"]
    assert "shared_common_photo_count" in record["evidence"]["photo_signal"]


def test_relationship_type_never_defaults_to_conflict_on_simultaneous_active_alone():
    """Чистая функция classify_relationship — simultaneous activity БЕЗ
    сильных общих признаков -> 'unknown', НЕ провоцирует reject где-либо
    (conflict_reasons вообще её не учитывает, см. _conflict_reasons)."""
    from bot.identity.property_linker import classify_relationship, _conflict_reasons

    anchor = {"address": "Безномерная улица", "rooms": 2, "seller_name": "А",
              "first_seen": NOW - timedelta(days=5), "archived_at": None, "price": 10_000_000}
    candidate = {"address": "Совсем Другая Улица", "rooms": 2, "seller_name": "Б",
                 "first_seen": NOW - timedelta(days=5), "archived_at": None, "price": 10_000_000}
    assert classify_relationship(anchor, candidate) == "unknown"
    assert _conflict_reasons(anchor, candidate) == []  # simultaneously_active НЕ в conflict_reasons
