"""Регрессия для Property Identity (задача 2026-08-16, "P1 — Property
Identity", миграции 083/084) — bot/identity/property_linker.py +
scripts/backfill_property_ids.py. Реальная БД, синтетические строки
(__test_pid_* id-скоуп) — не трогают реальные apartment_listings/
properties."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

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
                           first_seen=None, last_seen=None, archived_at=None):
    from bot.db.pg import execute
    await execute(
        """
        INSERT INTO apartment_listings (id, url, address, floor, area, rooms, complex_name,
                                         first_seen, last_seen, archived_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, COALESCE($8, now()), COALESCE($9, now()), $10)
        ON CONFLICT (id) DO UPDATE SET address=$3, floor=$4, area=$5, rooms=$6, complex_name=$7,
                                        first_seen=COALESCE($8, apartment_listings.first_seen),
                                        last_seen=COALESCE($9, apartment_listings.last_seen),
                                        archived_at=$10
        """,
        lid, f"https://krisha.kz/test/{lid}", address, floor, area, rooms, complex_name,
        first_seen, last_seen, archived_at,
    )


async def _insert_complex(name):
    from bot.db.pg import fetchval
    return await fetchval("INSERT INTO complexes (name, is_garbage) VALUES ($1, FALSE) RETURNING id", name)


async def _cleanup(*listing_ids, complex_ids=(), address_hashes=()):
    from bot.db.pg import execute
    await execute("DELETE FROM property_listings WHERE listing_id = ANY($1::text[])", list(listing_ids))
    if address_hashes:
        await execute("DELETE FROM properties WHERE address_hash = ANY($1::text[])", list(address_hashes))
    await execute("DELETE FROM apartment_listings WHERE id = ANY($1::text[])", list(listing_ids))
    if complex_ids:
        await execute("DELETE FROM complexes WHERE id = ANY($1::int[])", list(complex_ids))


def test_normalize_address_strips_noise_and_case():
    from bot.identity.property_linker import normalize_address
    a = normalize_address("г. Астана, ул. Тлендиева 21")
    b = normalize_address("Тлендиева 21")
    assert a == b == "тлендиева 21"


def test_compute_address_hash_none_when_field_missing():
    from bot.identity.property_linker import compute_address_hash
    assert compute_address_hash(None, 5, 60.0) is None
    assert compute_address_hash("Тлендиева 21", None, 60.0) is None
    assert compute_address_hash("Тлендиева 21", 5, None) is None
    assert compute_address_hash("", 5, 60.0) is None
    assert compute_address_hash("Тлендиева 21", 5, 60.0) is not None


@pytest.mark.asyncio
async def test_same_address_floor_area_gives_same_property(db):
    """Два listing с одинаковым адресом+этажом+площадью -> один
    property_id (задача, пункт 5, тест 1)."""
    from bot.identity.property_linker import link_listing_to_property, compute_address_hash

    lid_a, lid_b = "__test_pid_same_a__", "__test_pid_same_b__"
    await _insert_listing(lid_a, address="Тлендиева 21", floor=5, area=60.5, rooms=2)
    await _insert_listing(lid_b, address="Тлендиева 21", floor=5, area=60.5, rooms=2)
    h = compute_address_hash("Тлендиева 21", 5, 60.5)
    try:
        r1 = await link_listing_to_property({"id": lid_a, "address": "Тлендиева 21", "floor": 5,
                                              "area": 60.5, "rooms": 2, "complex_name": None})
        r2 = await link_listing_to_property({"id": lid_b, "address": "Тлендиева 21", "floor": 5,
                                              "area": 60.5, "rooms": 2, "complex_name": None})
        assert r1["property_id"] == r2["property_id"]
        assert r1["created"] is True
        assert r2["created"] is False
        assert r2["method"] == "auto"
    finally:
        await _cleanup(lid_a, lid_b, address_hashes=[h])


@pytest.mark.asyncio
async def test_different_address_gives_different_property(db):
    """Два listing с разным адресом -> разные property_id (задача,
    пункт 5, тест 2)."""
    from bot.identity.property_linker import link_listing_to_property, compute_address_hash

    lid_a, lid_b = "__test_pid_diff_a__", "__test_pid_diff_b__"
    await _insert_listing(lid_a, address="Тлендиева 21", floor=5, area=60.5)
    await _insert_listing(lid_b, address="Кабанбай батыра 15", floor=5, area=60.5)
    h1 = compute_address_hash("Тлендиева 21", 5, 60.5)
    h2 = compute_address_hash("Кабанбай батыра 15", 5, 60.5)
    try:
        r1 = await link_listing_to_property({"id": lid_a, "address": "Тлендиева 21", "floor": 5,
                                              "area": 60.5, "rooms": None, "complex_name": None})
        r2 = await link_listing_to_property({"id": lid_b, "address": "Кабанбай батыра 15", "floor": 5,
                                              "area": 60.5, "rooms": None, "complex_name": None})
        assert r1["property_id"] != r2["property_id"]
        assert r1["created"] is True and r2["created"] is True
    finally:
        await _cleanup(lid_a, lid_b, address_hashes=[h1, h2])


@pytest.mark.asyncio
async def test_fuzzy_match_within_tolerance_gives_confidence_below_one(db):
    """Тот же ЖК+этаж, площадь в пределах ±1м², но другое написание
    адреса (другой address_hash) -> fuzzy-match, confidence < 1.0
    (задача, пункт 5, тест 3)."""
    from bot.identity.property_linker import link_listing_to_property, compute_address_hash

    cid = await _insert_complex("__test_pid_fuzzy_complex__")
    lid_a, lid_b = "__test_pid_fuzzy_a__", "__test_pid_fuzzy_b__"
    await _insert_listing(lid_a, address="Тлендиева 21", floor=7, area=60.0, complex_name="__test_pid_fuzzy_complex__")
    await _insert_listing(lid_b, address="Тлендиева, дом 21", floor=7, area=60.8, complex_name="__test_pid_fuzzy_complex__")
    h1 = compute_address_hash("Тлендиева 21", 7, 60.0)
    h2 = compute_address_hash("Тлендиева, дом 21", 7, 60.8)
    try:
        r1 = await link_listing_to_property({"id": lid_a, "address": "Тлендиева 21", "floor": 7,
                                              "area": 60.0, "rooms": None, "complex_name": "__test_pid_fuzzy_complex__"})
        r2 = await link_listing_to_property({"id": lid_b, "address": "Тлендиева, дом 21", "floor": 7,
                                              "area": 60.8, "rooms": None, "complex_name": "__test_pid_fuzzy_complex__"})
        assert r1["created"] is True
        assert r2["method"] == "fuzzy"
        assert r2["property_id"] == r1["property_id"]
        assert r2["confidence"] is not None and r2["confidence"] < 1.0
    finally:
        await _cleanup(lid_a, lid_b, complex_ids=[cid], address_hashes=[h1, h2])


@pytest.mark.asyncio
async def test_fuzzy_match_outside_tolerance_creates_new_property(db):
    """Площадь за пределами ±1м² допуска -> НЕ fuzzy-match, отдельная
    новая квартира (граница допуска, регрессия на будущее расширение
    tolerance)."""
    from bot.identity.property_linker import link_listing_to_property, compute_address_hash

    cid = await _insert_complex("__test_pid_nofuzzy_complex__")
    lid_a, lid_b = "__test_pid_nofuzzy_a__", "__test_pid_nofuzzy_b__"
    await _insert_listing(lid_a, address="Тлендиева 21", floor=7, area=60.0, complex_name="__test_pid_nofuzzy_complex__")
    await _insert_listing(lid_b, address="Тлендиева, дом 21", floor=7, area=63.0, complex_name="__test_pid_nofuzzy_complex__")
    h1 = compute_address_hash("Тлендиева 21", 7, 60.0)
    h2 = compute_address_hash("Тлендиева, дом 21", 7, 63.0)
    try:
        r1 = await link_listing_to_property({"id": lid_a, "address": "Тлендиева 21", "floor": 7,
                                              "area": 60.0, "rooms": None, "complex_name": "__test_pid_nofuzzy_complex__"})
        r2 = await link_listing_to_property({"id": lid_b, "address": "Тлендиева, дом 21", "floor": 7,
                                              "area": 63.0, "rooms": None, "complex_name": "__test_pid_nofuzzy_complex__"})
        assert r2["method"] == "auto"
        assert r2["created"] is True
        assert r1["property_id"] != r2["property_id"]
    finally:
        await _cleanup(lid_a, lid_b, complex_ids=[cid], address_hashes=[h1, h2])


@pytest.mark.asyncio
async def test_missing_area_is_skipped_not_guessed(db):
    """Площадь неизвестна -> method='skipped', property_id=None (Unknown
    ≠ average — не гадаем на неполных данных)."""
    from bot.identity.property_linker import link_listing_to_property

    lid = "__test_pid_skip__"
    await _insert_listing(lid, address="Тлендиева 21", floor=5, area=None)
    try:
        r = await link_listing_to_property({"id": lid, "address": "Тлендиева 21", "floor": 5,
                                             "area": None, "rooms": None, "complex_name": None})
        assert r == {"property_id": None, "method": "skipped", "confidence": None, "created": False}
    finally:
        await _cleanup(lid)


@pytest.mark.asyncio
async def test_relink_same_listing_short_circuits_to_already_linked(db):
    """Повторный вызов link_listing_to_property на ТОТ ЖЕ listing_id не
    создаёт вторую строку property_listings и не пересчитывает решение."""
    from bot.db.pg import fetchval
    from bot.identity.property_linker import link_listing_to_property, compute_address_hash

    lid = "__test_pid_relink__"
    await _insert_listing(lid, address="Тлендиева 21", floor=5, area=60.5)
    h = compute_address_hash("Тлендиева 21", 5, 60.5)
    try:
        r1 = await link_listing_to_property({"id": lid, "address": "Тлендиева 21", "floor": 5,
                                              "area": 60.5, "rooms": None, "complex_name": None})
        r2 = await link_listing_to_property({"id": lid, "address": "Тлендиева 21", "floor": 5,
                                              "area": 60.5, "rooms": None, "complex_name": None})
        assert r2["method"] == "already_linked"
        assert r2["property_id"] == r1["property_id"]
        count = await fetchval("SELECT count(*) FROM property_listings WHERE listing_id = $1", lid)
        assert count == 1
    finally:
        await _cleanup(lid, address_hashes=[h])


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(db):
    """dry_run=True — ни properties, ни property_listings не меняются."""
    from bot.db.pg import fetchval
    from bot.identity.property_linker import link_listing_to_property, compute_address_hash, DryRunCache

    lid = "__test_pid_dryrun__"
    await _insert_listing(lid, address="Гагарина 30", floor=3, area=45.0)
    h = compute_address_hash("Гагарина 30", 3, 45.0)
    try:
        r = await link_listing_to_property(
            {"id": lid, "address": "Гагарина 30", "floor": 3, "area": 45.0, "rooms": None, "complex_name": None},
            dry_run=True, dry_run_cache=DryRunCache(),
        )
        assert r["created"] is True
        assert r["property_id"] is None  # dry-run: ничего реально не вставлено
        prop_count = await fetchval("SELECT count(*) FROM properties WHERE address_hash = $1", h)
        link_count = await fetchval("SELECT count(*) FROM property_listings WHERE listing_id = $1", lid)
        assert prop_count == 0
        assert link_count == 0
    finally:
        await _cleanup(lid)


@pytest.mark.asyncio
async def test_dry_run_cache_avoids_double_counting_new_property(db):
    """Два listing одной ещё не виденной квартиры В ОДНОМ dry-run —
    только ПЕРВЫЙ считается 'created', второй находит его через
    dry_run_cache (иначе relist новой квартиры задвоил бы счётчик 'new',
    ровно то, что вся задача призвана НЕ делать)."""
    from bot.identity.property_linker import link_listing_to_property, DryRunCache

    lid_a, lid_b = "__test_pid_drycache_a__", "__test_pid_drycache_b__"
    await _insert_listing(lid_a, address="Сатпаева 9", floor=2, area=50.0)
    await _insert_listing(lid_b, address="Сатпаева 9", floor=2, area=50.0)
    try:
        cache = DryRunCache()
        r1 = await link_listing_to_property(
            {"id": lid_a, "address": "Сатпаева 9", "floor": 2, "area": 50.0, "rooms": None, "complex_name": None},
            dry_run=True, dry_run_cache=cache)
        r2 = await link_listing_to_property(
            {"id": lid_b, "address": "Сатпаева 9", "floor": 2, "area": 50.0, "rooms": None, "complex_name": None},
            dry_run=True, dry_run_cache=cache)
        assert r1["created"] is True
        assert r2["created"] is False
    finally:
        await _cleanup(lid_a, lid_b)


@pytest.mark.asyncio
async def test_backfill_idempotent_no_duplicate_links(db):
    """scripts/backfill_property_ids.py::run_backfill() дважды на одном
    скоупе (задача, пункт 5, тест 4) — второй прогон: 0 новых properties,
    все листинги 'already_linked', property_listings без дублей
    (UNIQUE(listing_id) в основе, но проверяем и на уровне поведения)."""
    from bot.db.pg import fetchval
    from backfill_property_ids import run_backfill

    lid_a, lid_b = "__test_pid_bf_a__", "__test_pid_bf_b__"
    await _insert_listing(lid_a, address="Кенесары 40", floor=9, area=72.0)
    await _insert_listing(lid_b, address="Момышулы 12", floor=1, area=38.0)
    try:
        first = await run_backfill(listing_ids=[lid_a, lid_b])
        assert first["auto_new"] == 2
        assert first["already_linked"] == 0

        second = await run_backfill(listing_ids=[lid_a, lid_b])
        assert second["already_linked"] == 2
        assert second["auto_new"] == 0

        for lid in (lid_a, lid_b):
            count = await fetchval("SELECT count(*) FROM property_listings WHERE listing_id = $1", lid)
            assert count == 1
    finally:
        from bot.db.pg import execute, fetch
        # property_id'ы, реально созданные ЭТИМ тестом — снимаем ДО
        # удаления property_listings (ON DELETE CASCADE иначе не даст их
        # найти через связь), удаляем именно их, не трогая чужие orphan-
        # properties где-то ещё в БД.
        own_property_ids = [r["property_id"] for r in await fetch(
            "SELECT property_id FROM property_listings WHERE listing_id = ANY($1::text[])", [lid_a, lid_b])]
        await execute("DELETE FROM property_listings WHERE listing_id = ANY($1::text[])", [lid_a, lid_b])
        if own_property_ids:
            await execute("DELETE FROM properties WHERE property_id = ANY($1::int[])", own_property_ids)
        await execute("DELETE FROM apartment_listings WHERE id = ANY($1::text[])", [lid_a, lid_b])


@pytest.mark.asyncio
async def test_property_dates_reflect_real_listing_history_not_backfill_run_time(db):
    """first_seen_at/last_seen_at на properties — не 'сейчас, когда
    линковщик отработал', а реальная история из apartment_listings: MIN
    first_seen / MAX(archived_at или last_seen) по ВСЕМ listing_id этой
    квартиры (задача, пункт 6 — true DOM без этого бессмысленен: backfill
    почти всегда идёт по историческим данным, а не в момент публикации)."""
    from datetime import datetime, timedelta, timezone
    from bot.db.pg import fetchrow
    from bot.identity.property_linker import link_listing_to_property, compute_address_hash

    lid_old, lid_new = "__test_pid_dates_old__", "__test_pid_dates_new__"
    old_first = datetime(2025, 1, 10, tzinfo=timezone.utc)
    old_archived = datetime(2025, 2, 5, tzinfo=timezone.utc)   # relist #1: снят с публикации
    new_first = datetime(2025, 3, 1, tzinfo=timezone.utc)      # relist #2: та же квартира, новый listing_id
    new_last = datetime(2025, 3, 20, tzinfo=timezone.utc)      # всё ещё активен на эту дату
    h = compute_address_hash("Байтурсынова 5", 4, 55.0)
    await _insert_listing(lid_old, address="Байтурсынова 5", floor=4, area=55.0,
                           first_seen=old_first, last_seen=old_archived, archived_at=old_archived)
    await _insert_listing(lid_new, address="Байтурсынова 5", floor=4, area=55.0,
                           first_seen=new_first, last_seen=new_last, archived_at=None)
    try:
        r1 = await link_listing_to_property({
            "id": lid_old, "address": "Байтурсынова 5", "floor": 4, "area": 55.0,
            "rooms": None, "complex_name": None,
            "first_seen": old_first, "last_seen": old_archived, "archived_at": old_archived,
        })
        r2 = await link_listing_to_property({
            "id": lid_new, "address": "Байтурсынова 5", "floor": 4, "area": 55.0,
            "rooms": None, "complex_name": None,
            "first_seen": new_first, "last_seen": new_last, "archived_at": None,
        })
        assert r1["property_id"] == r2["property_id"]

        prop = await fetchrow(
            "SELECT first_seen_at, last_seen_at FROM properties WHERE property_id = $1", r1["property_id"])
        assert prop["first_seen_at"] == old_first, "должна остаться САМАЯ РАННЯЯ дата (первый relist)"
        assert prop["last_seen_at"] == new_last, "должна стать САМАЯ ПОЗДНЯЯ дата (второй relist, ещё активен)"
        true_dom_days = (prop["last_seen_at"] - prop["first_seen_at"]).days
        assert true_dom_days == (new_last - old_first).days  # ~69 дней, не 0
    finally:
        await _cleanup(lid_old, lid_new, address_hashes=[h])
