"""Регрессия для задачи 2026-08-17 ("поддерживать Property Identity
инкрементально для новых объявлений", после production backfill PR #8) —
bot/jobs/property_identity_incremental.py.

Подтверждено ПЕРЕД написанием этого файла (см. докстринг модуля job'а):
ни один live-сервис (в частности krisha-apartments.service) НЕ вызывает
bot.identity.property_linker.link_listing_to_property после вставки
apartment_listings — ни по графу вызовов, ни по живому наблюдению
(282 новых listing'а после рестарта парсера, 0 из них получили
property_listings). Значит — отдельный job, эти тесты его и покрывают."""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")
NOW = datetime(2026, 8, 17, tzinfo=timezone.utc)


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


# ── 1. Новые listing'и получают ОТДЕЛЬНЫЕ provisional properties ────────

@pytest.mark.asyncio
async def test_new_listings_get_separate_provisional_properties(db):
    from bot.jobs.property_identity_incremental import run_incremental
    from bot.identity.property_linker import compute_address_hash

    lid_a, lid_b = "__test_pii_sep_a__", "__test_pii_sep_b__"
    await _insert_listing(lid_a, address="Инкремент Адрес, 1", floor=5, area=45.0)
    await _insert_listing(lid_b, address="Совсем Другой Инкремент Адрес, 2", floor=8, area=60.0)
    h1 = compute_address_hash("Инкремент Адрес, 1", 5, 45.0)
    h2 = compute_address_hash("Совсем Другой Инкремент Адрес, 2", 8, 60.0)
    try:
        report = await run_incremental(listing_ids=[lid_a, lid_b])
        assert report["status"] == "ok"
        assert report["provisional_created"] == 2

        from bot.db.pg import fetchval
        pid_a = await fetchval("SELECT property_id FROM property_listings WHERE listing_id=$1", lid_a)
        pid_b = await fetchval("SELECT property_id FROM property_listings WHERE listing_id=$1", lid_b)
        assert pid_a is not None and pid_b is not None
        assert pid_a != pid_b  # НЕ hard link
    finally:
        await _cleanup(lid_a, lid_b, address_hashes=[h1, h2])


# ── 2. Два новых объявления видят друг друга только ПОСЛЕ конца Фазы A ──

@pytest.mark.asyncio
async def test_two_new_listings_find_each_other_after_phase_a(db):
    """Одна пара, тот же address_hash — Фаза B должна найти РОВНО одно
    exact_hash-ребро (не 0 — доказывает, что оба видны друг другу к
    моменту генерации кандидатов; не 2 — доказывает единственное
    направление, не задвоение)."""
    from bot.jobs.property_identity_incremental import run_incremental
    from bot.identity.property_linker import compute_address_hash

    lid_a, lid_b = "__test_pii_seek_a__", "__test_pii_seek_b__"
    await _insert_listing(lid_a, address="Видят Друг Друга, 3", floor=4, area=50.0)
    await _insert_listing(lid_b, address="Видят Друг Друга, 3", floor=4, area=50.0)
    h = compute_address_hash("Видят Друг Друга, 3", 4, 50.0)
    try:
        report = await run_incremental(listing_ids=[lid_a, lid_b])
        assert report["provisional_created"] == 2
        assert report["exact_candidates"] == 1

        from bot.db.pg import fetchval
        count = await fetchval(
            "SELECT count(*) FROM property_match_candidates WHERE listing_id = ANY($1::text[])",
            [lid_a, lid_b])
        assert count == 1  # ровно одна строка на пару, не 0 и не 2
    finally:
        await _cleanup(lid_a, lid_b, address_hashes=[h])


# ── 3. Новый listing видит СТАРЫЙ property ───────────────────────────────

@pytest.mark.asyncio
async def test_new_listing_finds_old_property(db):
    from bot.jobs.property_identity_incremental import run_incremental
    from bot.identity.property_linker import compute_address_hash

    lid_old, lid_new = "__test_pii_old_a__", "__test_pii_new_b__"
    await _insert_listing(lid_old, address="Старый Новый Адрес, 4", floor=6, area=55.0)
    h = compute_address_hash("Старый Новый Адрес, 4", 6, 55.0)
    try:
        old_report = await run_incremental(listing_ids=[lid_old])
        assert old_report["provisional_created"] == 1

        # "старый" теперь already_linked — вставляем "новый" ПОЗЖЕ, с тем
        # же address_hash, и прогоняем job ТОЛЬКО на нём (listing_ids
        # скоупит "unlinked", так что lid_old — уже не в выборке).
        await _insert_listing(lid_new, address="Старый Новый Адрес, 4", floor=6, area=55.0)
        new_report = await run_incremental(listing_ids=[lid_new])
        assert new_report["provisional_created"] == 1
        assert new_report["exact_candidates"] == 1

        from bot.db.pg import fetchval
        old_pid = await fetchval("SELECT property_id FROM property_listings WHERE listing_id=$1", lid_old)
        cand_pid = await fetchval(
            "SELECT candidate_property_id FROM property_match_candidates WHERE listing_id=$1", lid_new)
        assert cand_pid == old_pid  # нашёл именно старую property, не создал вторую виртуальную
    finally:
        await _cleanup(lid_old, lid_new, address_hashes=[h])


# ── 4. Старые пары НЕ пересчитываются повторно ───────────────────────────

@pytest.mark.asyncio
async def test_old_pairs_not_recreated_on_next_run(db):
    from bot.jobs.property_identity_incremental import run_incremental
    from bot.identity.property_linker import compute_address_hash
    from bot.db.pg import fetchval

    lid_a, lid_b = "__test_pii_stale_a__", "__test_pii_stale_b__"
    await _insert_listing(lid_a, address="Старая Пара, 5", floor=3, area=40.0)
    await _insert_listing(lid_b, address="Старая Пара, 5", floor=3, area=40.0)
    h = compute_address_hash("Старая Пара, 5", 3, 40.0)
    try:
        first = await run_incremental(listing_ids=[lid_a, lid_b])
        assert first["exact_candidates"] == 1
        count_after_first = await fetchval(
            "SELECT count(*) FROM property_match_candidates WHERE listing_id = ANY($1::text[])",
            [lid_a, lid_b])

        # Оба теперь already_linked -> "unlinked" для этой пары больше
        # пусто, повторный прогон (даже если бы listing_ids не сузил
        # выборку) не должен ничего пересчитать для НИХ.
        second = await run_incremental(listing_ids=[lid_a, lid_b])
        assert second["unlinked_found"] == 0
        assert second["exact_candidates"] == 0

        count_after_second = await fetchval(
            "SELECT count(*) FROM property_match_candidates WHERE listing_id = ANY($1::text[])",
            [lid_a, lid_b])
        assert count_after_second == count_after_first  # не задвоилось
    finally:
        await _cleanup(lid_a, lid_b, address_hashes=[h])


# ── 5. skipped повторно проверяется после заполнения полей ──────────────

@pytest.mark.asyncio
async def test_skipped_reprocessed_after_fields_filled(db):
    from bot.jobs.property_identity_incremental import run_incremental
    from bot.identity.property_linker import compute_address_hash
    from bot.db.pg import execute, fetchval

    lid = "__test_pii_skip_fill__"
    await _insert_listing(lid, address="Скип Адрес, 6", floor=None, area=45.0)  # floor неизвестен
    try:
        first = await run_incremental(listing_ids=[lid])
        assert first["provisional_created"] == 0
        assert first["skipped_total"] == 1
        assert "missing: floor" in first["skipped_by_reason"]

        linked = await fetchval("SELECT property_id FROM property_listings WHERE listing_id=$1", lid)
        assert linked is None

        # Поле заполнилось (парсер дозаполнил этаж позже) — следующий
        # прогон должен подхватить БЕЗ отдельного механизма "повторной
        # проверки" (unlinked определяется через NOT EXISTS property_
        # listings, не через отдельный флаг skipped).
        await execute("UPDATE apartment_listings SET floor=5 WHERE id=$1", lid)
        h = compute_address_hash("Скип Адрес, 6", 5, 45.0)
        second = await run_incremental(listing_ids=[lid])
        assert second["provisional_created"] == 1
        assert second["skipped_total"] == 0
    finally:
        await _cleanup(lid, address_hashes=[compute_address_hash("Скип Адрес, 6", 5, 45.0)])


# ── 6. Два параллельных запуска — второй блокируется ─────────────────────

@pytest.mark.asyncio
async def test_concurrent_runs_are_blocked_by_advisory_lock(db):
    from bot.jobs.property_identity_incremental import run_incremental, _acquire_lock, _release_lock

    lock_conn = await _acquire_lock()
    assert lock_conn is not None  # первый "экземпляр" держит лок
    try:
        report = await run_incremental(listing_ids=["__test_pii_nonexistent__"])
        assert report["status"] == "skipped_locked"
        assert report["unlinked_found"] == 0
    finally:
        await _release_lock(lock_conn)

    # Лок освобождён — теперь обычный прогон снова проходит.
    report_after = await run_incremental(listing_ids=["__test_pii_nonexistent__"])
    assert report_after["status"] == "ok"


# ── 7. Повторный запуск ничего не дублирует ───────────────────────────────

@pytest.mark.asyncio
async def test_repeated_run_is_idempotent(db):
    from bot.jobs.property_identity_incremental import run_incremental
    from bot.identity.property_linker import compute_address_hash
    from bot.db.pg import fetchval

    lid = "__test_pii_idem__"
    await _insert_listing(lid, address="Идемпотент Инкремент, 7", floor=2, area=38.0)
    h = compute_address_hash("Идемпотент Инкремент, 7", 2, 38.0)
    try:
        first = await run_incremental(listing_ids=[lid])
        second = await run_incremental(listing_ids=[lid])
        assert first["provisional_created"] == 1
        assert second["provisional_created"] == 0
        assert second["unlinked_found"] == 0

        count = await fetchval("SELECT count(*) FROM property_listings WHERE listing_id=$1", lid)
        assert count == 1
    finally:
        await _cleanup(lid, address_hashes=[h])


# ── 8. Порядок новых listing'ов не меняет candidate graph ───────────────

@pytest.mark.asyncio
async def test_order_independence_of_new_listings(db):
    """Тот же принцип, что уже доказан на bot/identity/property_linker.py
    уровне (bootstrap_all_provisional/generate_all_candidates, полная
    БД) — здесь дублируем на уровне job'а с его собственными building
    block'ами, порядок строк, переданных в bootstrap_all_provisional,
    варьируется явно (job сам всегда фиксирует ORDER BY al.id в _fetch_
    unlinked, но кандидат-граф не должен зависеть от порядка ДАЖЕ если
    бы он не фиксировал — это гарантия generate_all_candidates, не
    случайное совпадение с одним конкретным SQL ORDER BY)."""
    from bot.identity.property_linker import (
        bootstrap_all_provisional, generate_all_candidates, compute_address_hash,
    )
    from bot.db.pg import fetch

    cid = None
    lids = [f"__test_pii_order_{i}__" for i in range(4)]
    specs = [
        ("Джоб Порядок, 8", 4, 50.0), ("Джоб Порядок, 8", 4, 50.0),
        ("Джоб Порядок Б, 9", 9, 70.0), ("Джоб Порядок В, 10", 9, 70.3),
    ]
    hashes = []
    try:
        cid = await _insert_complex("__test_pii_order_complex__")
        for lid, (addr, floor, area) in zip(lids, specs):
            await _insert_listing(lid, address=addr, floor=floor, area=area,
                                   complex_name="__test_pii_order_complex__")
            hashes.append(compute_address_hash(addr, floor, area))

        _COLUMNS = ("id, address, floor, area, rooms, complex_name, first_seen, last_seen, archived_at, "
                    "price, seller_name, is_duplicate, duplicate_of, dup_match")
        all_rows = {r["id"]: dict(r) for r in await fetch(
            f"SELECT {_COLUMNS} FROM apartment_listings WHERE id = ANY($1::text[])", lids)}

        def canon(candidates):
            edges = {}
            for c in candidates:
                pair = frozenset([c["listing_id"], c["other_listing_id"]])
                edges[pair] = (c["match_method"], c["relationship_type"], c["status"])
            return edges

        orders = [list(lids), list(reversed(lids)), [lids[2], lids[0], lids[3], lids[1]]]
        graphs = []
        for order in orders:
            rows = [all_rows[lid] for lid in order]
            mapping = await bootstrap_all_provisional(rows, dry_run=True)
            candidates = await generate_all_candidates(mapping)
            graphs.append(canon(candidates))

        assert graphs[0] == graphs[1] == graphs[2]
        assert len(graphs[0]) >= 1  # не пустышка — реально что-то нашли
    finally:
        await _cleanup(*lids, complex_ids=[cid] if cid else [], address_hashes=hashes)


# ── 9. dry-run ничего не пишет ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dry_run_does_not_write(db):
    from bot.jobs.property_identity_incremental import run_incremental
    from bot.identity.property_linker import compute_address_hash
    from bot.db.pg import fetchval

    lid = "__test_pii_dryrun__"
    await _insert_listing(lid, address="Драйран Инкремент, 11", floor=1, area=30.0)
    h = compute_address_hash("Драйран Инкремент, 11", 1, 30.0)
    try:
        report = await run_incremental(dry_run=True, listing_ids=[lid])
        assert report["dry_run"] is True
        assert report["provisional_created"] == 1  # посчитал бы, но не вставил

        linked = await fetchval("SELECT property_id FROM property_listings WHERE listing_id=$1", lid)
        assert linked is None
        cand_count = await fetchval(
            "SELECT count(*) FROM property_match_candidates WHERE listing_id=$1", lid)
        assert cand_count == 0
    finally:
        await _cleanup(lid, address_hashes=[h])


# ── 10. Никаких legacy hard-link method'ов ────────────────────────────────

@pytest.mark.asyncio
async def test_no_legacy_hard_links_appear(db):
    from bot.jobs.property_identity_incremental import run_incremental
    from bot.identity.property_linker import compute_address_hash
    from bot.db.pg import fetch

    lid_a, lid_b = "__test_pii_nolegacy_a__", "__test_pii_nolegacy_b__"
    await _insert_listing(lid_a, address="Не Легаси Адрес, 12", floor=7, area=65.0)
    await _insert_listing(lid_b, address="Не Легаси Адрес, 12", floor=7, area=65.0)
    h = compute_address_hash("Не Легаси Адрес, 12", 7, 65.0)
    try:
        report = await run_incremental(listing_ids=[lid_a, lid_b])
        assert report["hard_links_total"] >= 0  # таблично-широкая метрика, не должна упасть

        rows = await fetch(
            "SELECT link_method FROM property_listings WHERE listing_id = ANY($1::text[])", [lid_a, lid_b])
        methods = {r["link_method"] for r in rows}
        assert methods == {"bootstrap"}  # НЕ 'auto'/'fuzzy' (legacy exact_only/fuzzy hard-link)
    finally:
        await _cleanup(lid_a, lid_b, address_hashes=[h])


# ── CLI: --since/--limit/--verbose существуют и работают ────────────────

def test_cli_flags_exist():
    import inspect
    from bot.jobs.property_identity_incremental import main
    src = inspect.getsource(main)
    for flag in ("--dry-run", "--limit", "--since", "--verbose"):
        assert flag in src


@pytest.mark.asyncio
async def test_since_flag_scopes_by_first_seen(db):
    from bot.jobs.property_identity_incremental import run_incremental
    from bot.identity.property_linker import compute_address_hash

    lid_old_ts, lid_new_ts = "__test_pii_since_old__", "__test_pii_since_new__"
    old_ts = NOW - timedelta(days=5)
    new_ts = NOW - timedelta(minutes=1)
    await _insert_listing(lid_old_ts, address="Синс Старый, 13", floor=2, area=33.0, first_seen=old_ts)
    await _insert_listing(lid_new_ts, address="Синс Новый, 14", floor=2, area=33.0, first_seen=new_ts)
    h1 = compute_address_hash("Синс Старый, 13", 2, 33.0)
    h2 = compute_address_hash("Синс Новый, 14", 2, 33.0)
    try:
        report = await run_incremental(
            listing_ids=[lid_old_ts, lid_new_ts], since=NOW - timedelta(hours=1))
        assert report["unlinked_found"] == 1  # только lid_new_ts прошёл фильтр --since
        assert report["provisional_created"] == 1
    finally:
        await _cleanup(lid_old_ts, lid_new_ts, address_hashes=[h1, h2])
