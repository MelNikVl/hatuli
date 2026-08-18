"""Регрессия для scripts/backfill_listing_floors.py (задача 2026-08-17,
"Missing floor + orphan audit", коммит 1). Тестовые строки — '__test_...__'
id, удаляются в finally (тот же паттерн, что tests/test_property_identity_
incremental.py)."""
import importlib.util
import os
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")


def _load_module():
    """scripts/ не пакет — импортируем по пути, тот же приём, что уже
    проверен вручную при разработке скрипта."""
    path = os.path.join(os.path.dirname(__file__), "..", "scripts", "backfill_listing_floors.py")
    spec = importlib.util.spec_from_file_location("backfill_listing_floors", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BLF = _load_module()


@pytest_asyncio.fixture
async def db():
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    yield
    await close_pool()


async def _insert_listing(lid, floor=None, description=None, photos=None, linked_property_id=None):
    from bot.db.pg import execute
    await execute(
        "INSERT INTO apartment_listings (id, url, floor, description, photos) "
        "VALUES ($1, $2, $3, $4, $5::jsonb) "
        "ON CONFLICT (id) DO UPDATE SET floor=$3, description=$4, photos=$5::jsonb",
        lid, f"https://krisha.kz/test/{lid}", floor, description,
        __import__("json").dumps(photos) if photos is not None else None,
    )
    if linked_property_id is not None:
        await execute(
            "INSERT INTO property_listings (property_id, listing_id, link_method, confidence) "
            "VALUES ($1, $2, 'auto', 1.0) ON CONFLICT (listing_id) DO NOTHING",
            linked_property_id, lid,
        )


async def _cleanup(*listing_ids):
    from bot.db.pg import execute
    await execute("DELETE FROM property_listings WHERE listing_id = ANY($1::text[])", list(listing_ids))
    await execute("DELETE FROM apartment_listings WHERE id = ANY($1::text[])", list(listing_ids))


# ── 1. classify_and_extract — чистая функция ──────────────────────────────

def test_classify_floor_filled():
    assert BLF.classify_and_extract({"floor": 5, "floors_total": 9}) == ("floor_filled", 5, 9)


def test_classify_floor_not_found():
    assert BLF.classify_and_extract({"title_full": "..."}) == ("floor_not_found", None, None)


def test_classify_unavailable_takes_priority_over_floor():
    # is_archived=True — даже если страница ЗАОДНО отдала floor, не доверяем
    # (задача: архивная страница ненадёжнее, listing и так под удаление).
    assert BLF.classify_and_extract({"is_archived": True, "floor": 5}) == ("unavailable", None, None)


# ── 1b. classify_and_extract — not_applicable (follow-up 2026-08-18, п.3) ──

def test_classify_flat_layout_is_not_applicable():
    assert BLF.classify_and_extract({"is_flat_layout": True}) == ("not_applicable", None, None)


def test_classify_flat_layout_takes_priority_over_everything():
    # is_flat_layout=True — даже если страница ЗАОДНО отдала floor и
    # НЕ архивна, всё равно not_applicable: карточка типа квартиры
    # структурно не про конкретную физическую единицу.
    assert BLF.classify_and_extract(
        {"is_flat_layout": True, "floor": 5, "is_archived": False}) == ("not_applicable", None, None)


def test_classify_not_flat_layout_falls_through_normally():
    assert BLF.classify_and_extract({"is_flat_layout": False, "floor": 5, "floors_total": 9}) == \
        ("floor_filled", 5, 9)


# ── 2. Выборка: только floor IS NULL И НЕТ property_listings ─────────────

@pytest.mark.asyncio
async def test_select_targets_excludes_already_linked(db):
    a, b = "__test_blf_unlinked__", "__test_blf_linked__"
    cid = None
    try:
        await _insert_listing(a, floor=None)
        await _insert_listing(b, floor=None)
        from bot.db.pg import fetchval, execute
        pid = await fetchval(
            "INSERT INTO properties (address_hash) VALUES ('__test_blf_hash__') RETURNING property_id")
        cid = pid
        await execute(
            "INSERT INTO property_listings (property_id, listing_id, link_method, confidence) "
            "VALUES ($1, $2, 'auto', 1.0)", pid, b)

        targets = await BLF._select_targets(None, None)
        ids = {t["id"] for t in targets}
        assert a in ids
        assert b not in ids  # floor IS NULL, но УЖЕ связан — не в выборке
    finally:
        await _cleanup(a, b)
        if cid is not None:
            from bot.db.pg import execute
            await execute("DELETE FROM properties WHERE property_id = $1", cid)


@pytest.mark.asyncio
async def test_select_targets_excludes_listing_with_floor_already_set(db):
    a = "__test_blf_has_floor__"
    try:
        await _insert_listing(a, floor=7)
        targets = await BLF._select_targets(None, None)
        assert a not in {t["id"] for t in targets}
    finally:
        await _cleanup(a)


@pytest.mark.asyncio
async def test_select_targets_listing_id_filter(db):
    a, b = "__test_blf_filter_a__", "__test_blf_filter_b__"
    try:
        await _insert_listing(a, floor=None)
        await _insert_listing(b, floor=None)
        targets = await BLF._select_targets(None, a)
        assert {t["id"] for t in targets} == {a}
    finally:
        await _cleanup(a, b)


# ── 3. dry-run не пишет в БД ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dry_run_does_not_write_floor(db):
    a = "__test_blf_dryrun__"
    try:
        await _insert_listing(a, floor=None)
        with patch("bot.core.apartment_details.fetch_apartment_details",
                   new=AsyncMock(return_value={"floor": 4, "floors_total": 9})):
            stats = await BLF.run_backfill(limit=None, batch_size=100, dry_run=True, listing_id=a)
        assert stats.floor_filled == 1
        from bot.db.pg import fetchval
        floor = await fetchval("SELECT floor FROM apartment_listings WHERE id=$1", a)
        assert floor is None  # dry-run — ничего не записано
    finally:
        await _cleanup(a)


# ── 4. Реальная запись пишет ТОЛЬКО floor/floors_total ────────────────────

@pytest.mark.asyncio
async def test_real_run_writes_only_floor_not_other_fields(db):
    a = "__test_blf_write__"
    try:
        await _insert_listing(a, floor=None, description="старое описание")
        with patch("bot.core.apartment_details.fetch_apartment_details",
                   new=AsyncMock(return_value={
                       "floor": 6, "floors_total": 12,
                       "description": "НОВОЕ описание от detail-fetch",
                       "photos": ["https://example.com/x.jpg"],
                   })):
            stats = await BLF.run_backfill(limit=None, batch_size=100, dry_run=False, listing_id=a)
        assert stats.floor_filled == 1
        from bot.db.pg import fetchrow
        row = await fetchrow(
            "SELECT floor, floors_total, description, photos FROM apartment_listings WHERE id=$1", a)
        assert row["floor"] == 6
        assert row["floors_total"] == 12
        # НЕ перезаписано — задача: "не перезаписывать другие заполненные поля"
        assert row["description"] == "старое описание"
        assert row["photos"] is None
    finally:
        await _cleanup(a)


# ── 5. floor IS NULL guard защищает от гонки ──────────────────────────────

@pytest.mark.asyncio
async def test_update_guarded_by_floor_is_null_does_not_clobber_concurrent_write(db):
    """Симулируем гонку: между SELECT (внутри run_backfill) и нашим UPDATE
    кто-то ДРУГОЙ (coord_backfill.py, живой парсер) уже поставил floor=3.
    WHERE floor IS NULL в UPDATE должен НЕ дать нашему результату (floor=9)
    его перезаписать."""
    a = "__test_blf_race__"
    try:
        await _insert_listing(a, floor=None)

        async def _fake_fetch(url, raise_on_error=False):
            # "конкурентная" запись происходит МЕЖДУ выборкой и нашим UPDATE
            from bot.db.pg import execute
            await execute("UPDATE apartment_listings SET floor=3 WHERE id=$1", a)
            return {"floor": 9, "floors_total": 20}

        with patch("bot.core.apartment_details.fetch_apartment_details", new=_fake_fetch):
            await BLF.run_backfill(limit=None, batch_size=100, dry_run=False, listing_id=a)

        from bot.db.pg import fetchval
        floor = await fetchval("SELECT floor FROM apartment_listings WHERE id=$1", a)
        assert floor == 3  # НЕ 9 — guard сработал, чужая запись не перезаписана
    finally:
        await _cleanup(a)


# ── 6. blocked/errors статистика ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_blocked_counted_separately_from_errors(db):
    from bot.core.apartment_details import ListingBlockedError
    a = "__test_blf_blocked__"
    try:
        await _insert_listing(a, floor=None)
        with patch("bot.core.apartment_details.fetch_apartment_details",
                   new=AsyncMock(side_effect=ListingBlockedError("403"))):
            with patch("asyncio.sleep", new=AsyncMock()):  # не ждать реальные backoff-паузы в тесте
                stats = await BLF.run_backfill(limit=None, batch_size=100, dry_run=True, listing_id=a)
        assert stats.blocked == 1
        assert stats.errors == 0
        assert stats.floor_filled == 0
    finally:
        await _cleanup(a)


@pytest.mark.asyncio
async def test_generic_error_counted_as_errors_not_blocked(db):
    a = "__test_blf_error__"
    try:
        await _insert_listing(a, floor=None)
        with patch("bot.core.apartment_details.fetch_apartment_details",
                   new=AsyncMock(side_effect=TimeoutError("boom"))):
            with patch("asyncio.sleep", new=AsyncMock()):
                stats = await BLF.run_backfill(limit=None, batch_size=100, dry_run=True, listing_id=a)
        assert stats.errors == 1
        assert stats.blocked == 0
    finally:
        await _cleanup(a)


# ── 7. verify_incremental_picks_up — только читает/dry-run, не пишет ─────

# ── 8. not_applicable — ничего не пишет, даже floor=None explicit ────────

@pytest.mark.asyncio
async def test_flat_layout_writes_nothing(db):
    a = "__test_blf_flatlayout__"
    try:
        await _insert_listing(a, floor=None, description="было")
        with patch("bot.core.apartment_details.fetch_apartment_details",
                   new=AsyncMock(return_value={"is_flat_layout": True, "floor": None})):
            stats = await BLF.run_backfill(limit=None, batch_size=100, dry_run=False, listing_id=a)
        assert stats.not_applicable == 1
        assert stats.floor_filled == 0
        from bot.db.pg import fetchrow
        row = await fetchrow("SELECT floor, description FROM apartment_listings WHERE id=$1", a)
        assert row["floor"] is None  # не фиктивное значение, просто не тронуто
        assert row["description"] == "было"
    finally:
        await _cleanup(a)


# ── 9. --order корректность (oldest/newest/random) ────────────────────────

@pytest.mark.asyncio
async def test_order_oldest_ascending_by_numeric_id(db):
    ids = ["__test_blf_ord_100__", "__test_blf_ord_200__", "__test_blf_ord_300__"]
    # намеренно нечисловые id (тестовый паттерн проекта) — сортировка по
    # numeric CASE не применяется к ним (NULLS LAST), но относительный
    # порядок среди СЕБЯ (все нечисловые) стабилен по al.id ASC/DESC —
    # тестируем через реальные числовые listing_id, вставленные как id.
    numeric_ids = ["9000000001", "9000000002", "9000000003"]
    try:
        for nid in numeric_ids:
            await _insert_listing(nid, floor=None)
        targets = await BLF._select_targets(None, None, order="oldest")
        mine = [t["id"] for t in targets if t["id"] in numeric_ids]
        assert mine == sorted(numeric_ids)  # по возрастанию численно
    finally:
        await _cleanup(*numeric_ids)


@pytest.mark.asyncio
async def test_order_newest_descending_by_numeric_id(db):
    numeric_ids = ["9000000011", "9000000012", "9000000013"]
    try:
        for nid in numeric_ids:
            await _insert_listing(nid, floor=None)
        targets = await BLF._select_targets(None, None, order="newest")
        mine = [t["id"] for t in targets if t["id"] in numeric_ids]
        assert mine == sorted(numeric_ids, reverse=True)
    finally:
        await _cleanup(*numeric_ids)


@pytest.mark.asyncio
async def test_order_random_is_reproducible_with_same_seed(db):
    numeric_ids = ["9000000021", "9000000022", "9000000023", "9000000024", "9000000025"]
    try:
        for nid in numeric_ids:
            await _insert_listing(nid, floor=None)
        t1 = await BLF._select_targets(None, None, order="random", seed=42)
        t2 = await BLF._select_targets(None, None, order="random", seed=42)
        mine1 = [t["id"] for t in t1 if t["id"] in numeric_ids]
        mine2 = [t["id"] for t in t2 if t["id"] in numeric_ids]
        assert mine1 == mine2  # тот же seed -> тот же порядок
    finally:
        await _cleanup(*numeric_ids)


@pytest.mark.asyncio
async def test_order_random_different_seeds_can_differ(db):
    """Не гарантирует РАЗНЫЙ порядок при разных seed (могло бы случайно
    совпасть) — проверяет только то, что seed реально участвует в запросе
    (разные параметры -> запрос не падает, оба возвращают полный набор)."""
    numeric_ids = [f"90000000{n}" for n in range(30, 40)]
    try:
        for nid in numeric_ids:
            await _insert_listing(nid, floor=None)
        t1 = await BLF._select_targets(None, None, order="random", seed=1)
        t2 = await BLF._select_targets(None, None, order="random", seed=2)
        mine1 = {t["id"] for t in t1 if t["id"] in numeric_ids}
        mine2 = {t["id"] for t in t2 if t["id"] in numeric_ids}
        assert mine1 == mine2 == set(numeric_ids)  # оба видят весь набор, просто порядок может отличаться
    finally:
        await _cleanup(*numeric_ids)


def test_invalid_order_rejected():
    import asyncio
    with pytest.raises(ValueError):
        asyncio.run(BLF._select_targets(None, None, order="bogus"))


# ── 10. Resume — второй прогон не трогает уже заполненные/недоступные ────

@pytest.mark.asyncio
async def test_resume_second_run_skips_already_resolved(db):
    """Первый прогон заполняет floor одному listing'у — второй прогон
    (без --listing-id, полная выборка) больше НЕ видит его (floor IS NOT
    NULL теперь) — тот же idempotent WHERE, никакой отдельный 'resume'
    флаг не нужен (задача, явно допускает: 'сохранить resume и
    идемпотентность' — тут это естественное свойство WHERE floor IS NULL)."""
    a, b = "__test_blf_resume_a__", "__test_blf_resume_b__"
    try:
        await _insert_listing(a, floor=None)
        await _insert_listing(b, floor=None)

        with patch("bot.core.apartment_details.fetch_apartment_details",
                   new=AsyncMock(return_value={"floor": 3, "floors_total": 9})):
            await BLF.run_backfill(limit=None, batch_size=100, dry_run=False, listing_id=a)

        # "продолжение" — полная выборка (без --listing-id) больше не видит `a`.
        targets = await BLF._select_targets(None, None)
        ids = {t["id"] for t in targets}
        assert a not in ids
        assert b in ids
    finally:
        await _cleanup(a, b)


@pytest.mark.asyncio
async def test_verify_incremental_is_dry_run_only(db):
    """Не должно создавать property_listings — только читает + вызывает
    incremental job с dry_run=True (задача: "не запускать полный property
    backfill, проверить, что incremental подбирает эти объявления штатно")."""
    a = "__test_blf_verify__"
    try:
        await _insert_listing(a, floor=5)  # floor уже есть, unlinked
        result = await BLF.verify_incremental_picks_up(sample_size=50)
        assert a in result.get("sample", []) or result.get("note")
        from bot.db.pg import fetchval
        linked = await fetchval("SELECT property_id FROM property_listings WHERE listing_id=$1", a)
        assert linked is None  # dry-run job не должен был ничего записать
    finally:
        await _cleanup(a)
