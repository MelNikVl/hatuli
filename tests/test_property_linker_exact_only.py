"""Регрессия для задачи 2026-08-16 ("безопасный deterministic exact-only
property linker") — bot/identity/property_linker.py::match_mode +
scripts/backfill_property_ids.py::--match-mode/--order-by. Прямое
продолжение audit(property-linker): fuzzy match quality audit (76.9%
high-risk, order-dependency дефект) — принцип: false positive merge
хуже false negative duplicate."""
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


# ── 1. Две квартиры одного ЖК/этажа/площади НЕ объединяются fuzzy ──────

@pytest.mark.asyncio
async def test_two_apartments_same_complex_floor_area_not_merged_by_default(db):
    from bot.identity.property_linker import link_listing_to_property, compute_address_hash

    cid = await _insert_complex("__test_eo_complex1__")
    lid_a, lid_b = "__test_eo_a1__", "__test_eo_b1__"
    await _insert_listing(lid_a, address="Момышулы 10", floor=5, area=45.0, complex_name="__test_eo_complex1__")
    await _insert_listing(lid_b, address="Кабанбай батыра 20", floor=5, area=45.5, complex_name="__test_eo_complex1__")
    h1 = compute_address_hash("Момышулы 10", 5, 45.0)
    h2 = compute_address_hash("Кабанбай батыра 20", 5, 45.5)
    try:
        r1 = await link_listing_to_property(
            {"id": lid_a, "address": "Момышулы 10", "floor": 5, "area": 45.0, "rooms": 2,
             "complex_name": "__test_eo_complex1__"}, match_mode="exact_only")
        r2 = await link_listing_to_property(
            {"id": lid_b, "address": "Кабанбай батыра 20", "floor": 5, "area": 45.5, "rooms": 2,
             "complex_name": "__test_eo_complex1__"}, match_mode="exact_only")
        assert r1["property_id"] != r2["property_id"]
        assert r2["method"] == "auto"
        assert r2["created"] is True
        assert r2["fuzzy_candidate"] is not None  # найден бы, но не связан
    finally:
        await _cleanup(lid_a, lid_b, complex_ids=[cid], address_hashes=[h1, h2])


# ── 2. exact-only создаёт отдельные properties ───────────────────────────

@pytest.mark.asyncio
async def test_exact_only_creates_new_property_for_each_previously_fuzzy_listing(db):
    """Задача: "11245 прежних fuzzy listings не должны автоматически
    уходить в skipped... они должны получить собственный property"."""
    from bot.identity.property_linker import link_listing_to_property, compute_address_hash

    cid = await _insert_complex("__test_eo_complex2__")
    lids = ["__test_eo_c2__", "__test_eo_c3__", "__test_eo_c4__"]
    addrs = ["Улица А 1", "Улица Б 2", "Улица В 3"]
    hashes = []
    try:
        for lid, addr in zip(lids, addrs):
            await _insert_listing(lid, address=addr, floor=8, area=60.0, rooms=2,
                                   complex_name="__test_eo_complex2__")
            hashes.append(compute_address_hash(addr, 8, 60.0))
        results = []
        for lid, addr in zip(lids, addrs):
            r = await link_listing_to_property(
                {"id": lid, "address": addr, "floor": 8, "area": 60.0, "rooms": 2,
                 "complex_name": "__test_eo_complex2__"}, match_mode="exact_only")
            results.append(r)
        assert all(r["method"] == "auto" and r["created"] for r in results)
        assert len({r["property_id"] for r in results}) == 3  # 3 РАЗНЫХ property, не 1
        assert all(r["method"] != "skipped" for r in results)
    finally:
        await _cleanup(*lids, complex_ids=[cid], address_hashes=hashes)


# ── 3. Точный безопасный hash связывает relist ───────────────────────────

@pytest.mark.asyncio
async def test_exact_hash_still_links_safe_relist(db):
    from bot.identity.property_linker import link_listing_to_property, compute_address_hash

    lid_a, lid_b = "__test_eo_relist_a__", "__test_eo_relist_b__"
    await _insert_listing(lid_a, address="Тлендиева 21", floor=5, area=45.0)
    await _insert_listing(lid_b, address="Тлендиева 21", floor=5, area=45.0)  # тот же адрес — relist
    h = compute_address_hash("Тлендиева 21", 5, 45.0)
    try:
        r1 = await link_listing_to_property(
            {"id": lid_a, "address": "Тлендиева 21", "floor": 5, "area": 45.0, "rooms": None, "complex_name": None},
            match_mode="exact_only")
        r2 = await link_listing_to_property(
            {"id": lid_b, "address": "Тлендиева 21", "floor": 5, "area": 45.0, "rooms": None, "complex_name": None},
            match_mode="exact_only")
        assert r1["created"] is True
        assert r2["created"] is False
        assert r2["method"] == "auto"
        assert r2["property_id"] == r1["property_id"]
    finally:
        await _cleanup(lid_a, lid_b, address_hashes=[h])


# ── 4. Недостаточно данных -> skipped с причиной ─────────────────────────

@pytest.mark.asyncio
async def test_insufficient_data_skipped_with_specific_reason(db):
    from bot.identity.property_linker import link_listing_to_property

    lid = "__test_eo_skip__"
    await _insert_listing(lid, address="Тлендиева 21", floor=None, area=None)
    try:
        r = await link_listing_to_property(
            {"id": lid, "address": "Тлендиева 21", "floor": None, "area": None,
             "rooms": None, "complex_name": None})
        assert r["method"] == "skipped"
        assert r["property_id"] is None
        assert r["skip_reason"] is not None
        assert "floor" in r["skip_reason"] and "area" in r["skip_reason"]
        assert "address" not in r["skip_reason"]  # адрес-то как раз есть
    finally:
        await _cleanup(lid)


def test_skip_reason_pure_function():
    from bot.identity.property_linker import _skip_reason

    assert _skip_reason(None, 5, 45.0) == "missing: address"
    assert _skip_reason("Тлендиева 21", None, 45.0) == "missing: floor"
    assert _skip_reason("Тлендиева 21", 5, None) == "missing: area"
    assert _skip_reason(None, None, None) == "missing: address, floor, area"
    assert _skip_reason("Тлендиева 21", 5, 45.0) == "unknown"  # всё есть -> hash не None, сюда не попадём


# ── 5. Повторный запуск идемпотентен ──────────────────────────────────────

@pytest.mark.asyncio
async def test_repeated_exact_only_run_is_idempotent(db):
    from backfill_property_ids import run_backfill

    lids = ["__test_eo_idem_a__", "__test_eo_idem_b__", "__test_eo_idem_c__"]
    await _insert_listing(lids[0], address="Идем 1", floor=3, area=40.0)
    await _insert_listing(lids[1], address="Идем 1", floor=3, area=40.0)  # relist
    await _insert_listing(lids[2], address="Идем 2", floor=3, area=41.0)
    from bot.identity.property_linker import compute_address_hash
    hashes = [compute_address_hash("Идем 1", 3, 40.0), compute_address_hash("Идем 2", 3, 41.0)]
    try:
        first = await run_backfill(dry_run=False, listing_ids=lids, match_mode="exact_only")
        second = await run_backfill(dry_run=False, listing_ids=lids, match_mode="exact_only")

        assert first["auto_new"] == 2   # 2 уникальных адреса
        assert first["auto_existing"] == 1  # relist
        assert second["already_linked"] == 3  # ВСЕ уже связаны
        assert second["auto_new"] == 0
        assert second["auto_existing"] == 0
        assert first["properties_total_after"] == second["properties_total_after"]
    finally:
        await _cleanup(*lids, address_hashes=hashes)


# ── 6. Результат не зависит от порядка ────────────────────────────────────

@pytest.mark.asyncio
async def test_exact_only_order_independent_via_dry_run(db):
    """Реальный async-путь (не симуляция) — dry-run exact-only в 4
    разных порядках на одном наборе listing_id (задача, п.3)."""
    from backfill_property_ids import run_backfill
    from bot.identity.property_linker import compute_address_hash

    cid = await _insert_complex("__test_eo_order_complex__")
    lids = [f"__test_eo_order_{i}__" for i in range(6)]
    specs = [
        ("Заказ А 1", 4, 50.0), ("Заказ А 1", 4, 50.0),  # relist пара (тот же hash)
        ("Заказ Б 2", 4, 50.5),  # fuzzy-адъюнкт к паре выше (тот же ЖК/этаж/площадь-допуск), но exact_only -> отдельно
        ("Заказ В 3", 9, 70.0), ("Заказ В 3", 9, 70.0),  # ещё одна relist пара
        ("Заказ Г 4", 12, 33.0),
    ]
    hashes = []
    try:
        for lid, (addr, floor, area) in zip(lids, specs):
            await _insert_listing(lid, address=addr, floor=floor, area=area,
                                   complex_name="__test_eo_order_complex__")
            hashes.append(compute_address_hash(addr, floor, area))

        results = {}
        for order in ("id", "id_desc", "listed_at", "shuffled_42"):
            results[order] = await run_backfill(
                dry_run=True, listing_ids=lids, match_mode="exact_only", order_by=order)

        reference = results["id"]
        for order, stats in results.items():
            assert stats["auto_new"] == reference["auto_new"] == 4  # 4 уникальных hash
            assert stats["auto_existing"] == reference["auto_existing"] == 2  # 2 relist'а
            assert stats["fuzzy"] == 0  # exact_only НИКОГДА не hard-линкует fuzzy
            assert stats["skipped"] == reference["skipped"] == 0
    finally:
        await _cleanup(*lids, complex_ids=[cid], address_hashes=hashes)


# ── 7. Fuzzy-кандидат НЕ создаёт hard link ────────────────────────────────

@pytest.mark.asyncio
async def test_fuzzy_candidate_never_writes_property_listings_row_in_exact_only(db):
    from bot.db.pg import fetchval
    from bot.identity.property_linker import link_listing_to_property, compute_address_hash

    cid = await _insert_complex("__test_eo_nohardlink_complex__")
    lid_a, lid_b = "__test_eo_nhl_a__", "__test_eo_nhl_b__"
    await _insert_listing(lid_a, address="Хардлинк А", floor=6, area=55.0, complex_name="__test_eo_nohardlink_complex__")
    await _insert_listing(lid_b, address="Хардлинк Б", floor=6, area=55.8, complex_name="__test_eo_nohardlink_complex__")
    h1 = compute_address_hash("Хардлинк А", 6, 55.0)
    h2 = compute_address_hash("Хардлинк Б", 6, 55.8)
    try:
        r1 = await link_listing_to_property(
            {"id": lid_a, "address": "Хардлинк А", "floor": 6, "area": 55.0, "rooms": None,
             "complex_name": "__test_eo_nohardlink_complex__"}, match_mode="exact_only")
        r2 = await link_listing_to_property(
            {"id": lid_b, "address": "Хардлинк Б", "floor": 6, "area": 55.8, "rooms": None,
             "complex_name": "__test_eo_nohardlink_complex__"}, match_mode="exact_only")
        assert r2["fuzzy_candidate"] is not None  # кандидат БЫЛ найден
        # Но НИ ОДНОЙ строки property_listings с link_method='fuzzy' —
        # никогда не создавался hard link на его основе.
        fuzzy_rows = await fetchval(
            "SELECT count(*) FROM property_listings WHERE link_method = 'fuzzy' "
            "AND listing_id = ANY($1::text[])", [lid_a, lid_b])
        assert fuzzy_rows == 0
        # b получил СВОЮ, отдельную property (auto, не fuzzy).
        method = await fetchval("SELECT link_method FROM property_listings WHERE listing_id = $1", lid_b)
        assert method == "auto"
    finally:
        await _cleanup(lid_a, lid_b, complex_ids=[cid], address_hashes=[h1, h2])


# ── 8. Старый unsafe fuzzy режим нельзя случайно включить по умолчанию ──

@pytest.mark.asyncio
async def test_explicit_exact_only_mode_is_respected(db):
    """ПЕРЕИМЕНОВАНО (задача 2026-08-16, "безопасная инфраструктура
    кандидатов" — сменила ДЕФОЛТ на "candidate_only", exact_only больше
    не дефолт нигде, см. tests/test_property_linker_candidate_only.py::
    test_default_match_mode_is_candidate_only для актуальной проверки
    дефолта). Этот тест теперь проверяет, что ЯВНО запрошенный exact_only
    честно так себя и называет (не тихо деградирует в другой режим)."""
    from bot.identity.property_linker import link_listing_to_property

    lid = "__test_eo_default_mode__"
    await _insert_listing(lid, address="Дефолт 1", floor=1, area=30.0)
    try:
        r = await link_listing_to_property(
            {"id": lid, "address": "Дефолт 1", "floor": 1, "area": 30.0, "rooms": None, "complex_name": None},
            match_mode="exact_only")
        assert r["match_mode"] == "exact_only"
    finally:
        from bot.identity.property_linker import compute_address_hash
        await _cleanup(lid, address_hashes=[compute_address_hash("Дефолт 1", 1, 30.0)])


@pytest.mark.asyncio
async def test_invalid_match_mode_raises_value_error(db):
    """Опечатка ("exact"/"fuzy"/"") не должна тихо деградировать в
    какой-то режим — падает явно."""
    from bot.identity.property_linker import link_listing_to_property

    with pytest.raises(ValueError):
        await link_listing_to_property(
            {"id": "__test_eo_bad_mode__", "address": "х", "floor": 1, "area": 1.0,
             "rooms": None, "complex_name": None},
            match_mode="exact",  # НЕ валидное значение (правильное — "exact_only")
        )


def test_exact_only_remains_a_valid_explicit_choice():
    """ПЕРЕИМЕНОВАНО (см. test_explicit_exact_only_mode_is_respected
    выше) — дефолт entrypoint'а теперь "candidate_only"
    (tests/test_property_linker_candidate_only.py проверяет ЭТО),
    "exact_only" остаётся валидным ЯВНЫМ выбором (не удалён из
    _VALID_MATCH_MODES при смене дефолта)."""
    from bot.identity.property_linker import _VALID_MATCH_MODES

    assert "exact_only" in _VALID_MATCH_MODES
    assert "fuzzy" in _VALID_MATCH_MODES
