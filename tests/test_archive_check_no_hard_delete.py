"""Регрессия для bot/core/archive_check.py (задача 2026-08-17, follow-up
после production-деплоя "Property Identity — photo evidence + review":
scripts/audit_orphan_properties.py, коммит 181636e, нашёл 17 осиротевших
properties — 15/17 согласуются с тем, что check_archived() делал `DELETE
FROM apartment_listings` на подтверждённое 404/410 ("deleted"), каскадом
убирая property_listings (FK ON DELETE CASCADE), но НЕ properties
(родитель) — вся история, завязанная на listing_id, терялась навсегда.

Фикс: 'deleted' и 'archived' теперь ОДНО и то же действие с нашей стороны
— мягкая архивация (is_active=FALSE, archived_at=now()), различаются
только archive_reason (migrations/089). Физический DELETE НИКОГДА не
выполняется этим модулем — тесты явно это проверяют перехватом execute(),
не только косвенно через "строка ещё существует".

Тестовые строки — '__test_...__' id, удаляются в finally. _select_
candidates() и _check_one() ПАТЧАТСЯ (не гоняем реальный check_archived()
на живой таблице/сети — тот же принцип, что tests/test_archive_check_
pools.py уже документирует про 42к+ реальных строк, но здесь ЕЩЁ и сеть,
а не только выборка)."""
import os
import sys
from unittest.mock import AsyncMock, patch

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


async def _insert_listing(lid, property_id=None):
    from bot.db.pg import execute
    await execute(
        "INSERT INTO apartment_listings (id, url, is_active) VALUES ($1, $2, TRUE) "
        "ON CONFLICT (id) DO UPDATE SET is_active = TRUE, archived_at = NULL, archive_reason = NULL",
        lid, f"https://krisha.kz/test/{lid}",
    )
    if property_id is not None:
        await execute(
            "INSERT INTO property_listings (property_id, listing_id, link_method, confidence) "
            "VALUES ($1, $2, 'bootstrap', 1.0) ON CONFLICT (listing_id) DO NOTHING",
            property_id, lid,
        )


async def _cleanup(*listing_ids, property_ids=()):
    from bot.db.pg import execute
    await execute("DELETE FROM property_listings WHERE listing_id = ANY($1::text[])", list(listing_ids))
    if property_ids:
        await execute("DELETE FROM properties WHERE property_id = ANY($1::int[])", list(property_ids))
    await execute("DELETE FROM apartment_listings WHERE id = ANY($1::text[])", list(listing_ids))


async def _run_check_archived_for(row, result):
    """check_archived(), но _select_candidates подменена на ОДНУ заданную
    строку и _check_one — на заданный результат, БЕЗ реальной сети и БЕЗ
    риска задеть реальные production-строки. reactivate_reappeared_
    listings() ТОЖЕ подменена (no-op) — иначе check_archived() своим
    ПЕРВЫМ шагом реально реактивировал бы реальный прод-бэклог (211 строк
    на момент написания, is_active=FALSE И last_seen > archived_at) на
    каждый вызов этого хелпера; реактивация тестируется ОТДЕЛЬНО, см.
    test_reactivate_reappeared_listings_* ниже, через listing_ids-скоуп."""
    from bot.core import archive_check

    with patch.object(archive_check, "_select_candidates", new=AsyncMock(return_value=[row])), \
         patch.object(archive_check, "_check_one", new=AsyncMock(return_value=result)), \
         patch.object(archive_check, "reactivate_reappeared_listings", new=AsyncMock(return_value=[])), \
         patch.object(archive_check.asyncio, "sleep", new=AsyncMock()):
        return await archive_check.check_archived(limit=1)


async def _run_check_archived_rentals_for(row, result, url_map):
    from bot.core import archive_check
    from bot.db.pg import fetch as real_fetch

    async def _fake_fetch(sql, *args):
        if "rental_listings" in sql:
            return [row]
        return await real_fetch(sql, *args)

    with patch("bot.core.archive_check.fetch", new=_fake_fetch), \
         patch.object(archive_check, "_check_one", new=AsyncMock(return_value=result)), \
         patch.object(archive_check.asyncio, "sleep", new=AsyncMock()):
        return await archive_check.check_archived_rentals(limit=1)


# ── apartment_listings: 'deleted' больше НЕ удаляет строку ───────────────

@pytest.mark.asyncio
async def test_deleted_result_soft_archives_not_hard_deletes(db):
    lid = "__test_ac_deleted__"
    await _insert_listing(lid)
    try:
        delete_calls = []
        from bot.db.pg import execute as real_execute
        async def _spy_execute(sql, *args):
            if "DELETE FROM apartment_listings" in sql:
                delete_calls.append(sql)
            return await real_execute(sql, *args)

        with patch("bot.core.archive_check.execute", new=_spy_execute):
            report = await _run_check_archived_for({"id": lid, "url": f"https://krisha.kz/test/{lid}", "pool": "hot"},
                                                     "deleted")

        assert delete_calls == []  # НИКАКОГО DELETE FROM apartment_listings
        assert report["archived"] == 1

        from bot.db.pg import fetchrow
        row = await fetchrow(
            "SELECT is_active, archived_at, archive_reason FROM apartment_listings WHERE id = $1", lid)
        assert row is not None  # строка НЕ удалена
        assert row["is_active"] is False
        assert row["archived_at"] is not None
        assert row["archive_reason"] == "confirmed_gone"
    finally:
        await _cleanup(lid)


@pytest.mark.asyncio
async def test_archived_badge_result_soft_archives_with_different_reason(db):
    lid = "__test_ac_badge__"
    await _insert_listing(lid)
    try:
        report = await _run_check_archived_for({"id": lid, "url": f"https://krisha.kz/test/{lid}", "pool": "hot"},
                                                 "archived")
        assert report["archived"] == 1

        from bot.db.pg import fetchrow
        row = await fetchrow(
            "SELECT is_active, archived_at, archive_reason FROM apartment_listings WHERE id = $1", lid)
        assert row is not None
        assert row["is_active"] is False
        assert row["archive_reason"] == "archived_badge"
    finally:
        await _cleanup(lid)


@pytest.mark.asyncio
async def test_alive_result_does_not_set_archive_reason(db):
    """'alive' — только archive_checked_at, никакой архивации (поведение
    НЕ менялось этим фиксом, регрессия на побочный эффект)."""
    lid = "__test_ac_alive__"
    await _insert_listing(lid)
    try:
        await _run_check_archived_for({"id": lid, "url": f"https://krisha.kz/test/{lid}", "pool": "hot"}, "alive")

        from bot.db.pg import fetchrow
        row = await fetchrow(
            "SELECT is_active, archived_at, archive_reason, archive_checked_at FROM apartment_listings WHERE id = $1",
            lid)
        assert row["is_active"] is True
        assert row["archived_at"] is None
        assert row["archive_reason"] is None
        assert row["archive_checked_at"] is not None
    finally:
        await _cleanup(lid)


# ── property_listings/properties — сохраняются целиком (главная причина
#    фикса, задача явно требует "с сохранением property_listings и всей
#    истории") ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_confirmed_gone_listing_keeps_property_listings_link(db):
    from bot.db.pg import execute, fetchval
    lid = "__test_ac_orphan_guard__"
    prop_id = await fetchval(
        "INSERT INTO properties (address_hash) VALUES ('__test_ac_orphan_guard_hash__') RETURNING property_id")
    await _insert_listing(lid, property_id=prop_id)
    try:
        await _run_check_archived_for({"id": lid, "url": f"https://krisha.kz/test/{lid}", "pool": "hot"}, "deleted")

        linked = await fetchval("SELECT property_id FROM property_listings WHERE listing_id = $1", lid)
        assert linked == prop_id  # НЕ каскадом убрано — listing не удалён, значит и связь жива

        prop_alive = await fetchval("SELECT property_id FROM properties WHERE property_id = $1", prop_id)
        assert prop_alive == prop_id  # родитель тем более цел (и раньше не удалялся)
    finally:
        await execute("DELETE FROM property_listings WHERE listing_id = $1", lid)
        await execute("DELETE FROM properties WHERE property_id = $1", prop_id)
        await _cleanup(lid)


# ── rental_listings — тот же класс фикса ──────────────────────────────────

@pytest.mark.asyncio
async def test_rental_deleted_result_soft_archives_not_hard_deletes(db):
    from bot.db.pg import execute, fetchrow
    lid = "__test_ac_rental_deleted__"
    await execute(
        "INSERT INTO rental_listings (id, url, is_active) VALUES ($1, $2, TRUE) "
        "ON CONFLICT (id) DO UPDATE SET is_active = TRUE, archived_at = NULL, archive_reason = NULL",
        lid, f"https://krisha.kz/test/{lid}",
    )
    try:
        delete_calls = []
        real_execute = execute
        async def _spy_execute(sql, *args):
            if "DELETE FROM rental_listings" in sql:
                delete_calls.append(sql)
            return await real_execute(sql, *args)

        with patch("bot.core.archive_check.execute", new=_spy_execute):
            report = await _run_check_archived_rentals_for(
                {"id": lid, "url": f"https://krisha.kz/test/{lid}"}, "deleted", {})

        assert delete_calls == []
        assert report["archived"] == 1

        row = await fetchrow(
            "SELECT is_active, archived_at, archive_reason FROM rental_listings WHERE id = $1", lid)
        assert row is not None
        assert row["is_active"] is False
        assert row["archive_reason"] == "confirmed_gone"
    finally:
        await execute("DELETE FROM rental_listings WHERE id = $1", lid)


# ── Идемпотентность: повторный confirmed-404/badge НЕ двигает archived_at
#    вперёд, last_seen эта функция вообще не трогает (задача, явно) ───────

@pytest.mark.asyncio
async def test_repeated_confirmed_404_does_not_move_archived_at_or_last_seen(db):
    from datetime import datetime, timedelta, timezone
    from bot.db.pg import execute, fetchrow

    lid = "__test_ac_idempotent__"
    first_seen_last_seen = datetime.now(timezone.utc) - timedelta(days=5)
    await execute(
        "INSERT INTO apartment_listings (id, url, is_active, last_seen) VALUES ($1, $2, TRUE, $3) "
        "ON CONFLICT (id) DO UPDATE SET is_active = TRUE, archived_at = NULL, archive_reason = NULL, "
        "last_seen = $3",
        lid, f"https://krisha.kz/test/{lid}", first_seen_last_seen,
    )
    try:
        # Первое подтверждение "объявления больше нет" — archived_at ставится.
        await _run_check_archived_for({"id": lid, "url": f"https://krisha.kz/test/{lid}", "pool": "hot"}, "deleted")
        row1 = await fetchrow(
            "SELECT archived_at, last_seen, archive_checked_at FROM apartment_listings WHERE id = $1", lid)
        first_archived_at = row1["archived_at"]
        assert first_archived_at is not None
        assert row1["last_seen"] == first_seen_last_seen  # не тронут архивацией вообще

        # Второе подтверждение той же самой строки (тест намеренно зовёт
        # check_archived() ещё раз на ту же строку напрямую — в норме
        # _select_candidates её бы уже не выбрала, is_active IS NOT FALSE
        # её исключает, но UPDATE обязан быть идемпотентным сам по себе,
        # не полагаясь только на выборку выше).
        await _run_check_archived_for({"id": lid, "url": f"https://krisha.kz/test/{lid}", "pool": "hot"}, "deleted")
        row2 = await fetchrow(
            "SELECT archived_at, last_seen, archive_checked_at FROM apartment_listings WHERE id = $1", lid)

        assert row2["archived_at"] == first_archived_at  # НЕ сдвинут вторым подтверждением
        assert row2["last_seen"] == first_seen_last_seen  # по-прежнему не тронут
        assert row2["archive_checked_at"] >= row1["archive_checked_at"]  # это поле ОБНОВЛЯЕТСЯ, и это ожидаемо
    finally:
        await _cleanup(lid)


# ── Реактивация: объявление снова появилось (last_seen продвинулся мимо
#    archived_at) -> is_active=TRUE, archived_at/archive_reason очищены ──

@pytest.mark.asyncio
async def test_reactivate_reappeared_listings_reactivates_and_clears_reason(db):
    from datetime import datetime, timedelta, timezone
    from bot.core.archive_check import reactivate_reappeared_listings
    from bot.db.pg import execute, fetchrow

    lid = "__test_ac_reappeared__"
    archived_at = datetime.now(timezone.utc) - timedelta(days=2)
    last_seen = datetime.now(timezone.utc)  # ПОЗЖЕ archived_at — парсер видел его снова
    old_checked_at = archived_at
    await execute("""
        INSERT INTO apartment_listings (id, url, is_active, archived_at, archive_reason, last_seen,
                                         archive_checked_at)
        VALUES ($1, $2, FALSE, $3, 'confirmed_gone', $4, $5)
        ON CONFLICT (id) DO UPDATE SET is_active = FALSE, archived_at = $3,
            archive_reason = 'confirmed_gone', last_seen = $4, archive_checked_at = $5
    """, lid, f"https://krisha.kz/test/{lid}", archived_at, last_seen, old_checked_at)
    try:
        reactivated = await reactivate_reappeared_listings(listing_ids=[lid])
        assert reactivated == [lid]

        row = await fetchrow(
            "SELECT is_active, archived_at, archive_reason, last_seen, archive_checked_at "
            "FROM apartment_listings WHERE id = $1", lid)
        assert row["is_active"] is True
        assert row["archived_at"] is None
        assert row["archive_reason"] is None
        assert row["last_seen"] == last_seen  # реактивация last_seen не меняет, только читает его
        # archive_checked_at ОБНУЛЯЕТСЯ (не оставлен старым) — задача,
        # найдено read-only аудитом: "last_seen > archived_at" подтвердило
        # реактивацию только у 50% реальных примеров (см. докстринг функции)
        # -> реактивация ставит ГИПОТЕЗУ и форсирует немедленную реальную
        # перепроверку через backlog-ветку _select_candidates (та явно
        # фильтрует archive_checked_at IS NULL), а не тихо считает вопрос
        # решённым по одному устаревшему сравнению дат.
        assert row["archive_checked_at"] is None

        # История прежней архивации НЕ потеряна (задача, follow-up) —
        # старые archived_at/archive_reason сохранены в listing_archive_history.
        hist = await fetchrow(
            "SELECT archived_at, archive_reason, reactivated_at FROM listing_archive_history WHERE listing_id = $1",
            lid)
        assert hist is not None
        assert hist["archived_at"] == archived_at
        assert hist["archive_reason"] == "confirmed_gone"
        assert hist["reactivated_at"] is not None
    finally:
        await _cleanup(lid)


@pytest.mark.asyncio
async def test_reactivate_is_idempotent_no_duplicate_history(db):
    """Задача, явно: "сделай операцию идемпотентной" — повторный вызов
    на ту же (уже реактивированную) строку не находит кандидатов и не
    пишет вторую строку истории."""
    from datetime import datetime, timedelta, timezone
    from bot.core.archive_check import reactivate_reappeared_listings
    from bot.db.pg import execute, fetchval

    lid = "__test_ac_reactivate_idempotent__"
    archived_at = datetime.now(timezone.utc) - timedelta(days=2)
    last_seen = datetime.now(timezone.utc)
    await execute("""
        INSERT INTO apartment_listings (id, url, is_active, archived_at, archive_reason, last_seen)
        VALUES ($1, $2, FALSE, $3, 'confirmed_gone', $4)
        ON CONFLICT (id) DO UPDATE SET is_active = FALSE, archived_at = $3,
            archive_reason = 'confirmed_gone', last_seen = $4
    """, lid, f"https://krisha.kz/test/{lid}", archived_at, last_seen)
    try:
        first = await reactivate_reappeared_listings(listing_ids=[lid])
        assert first == [lid]
        second = await reactivate_reappeared_listings(listing_ids=[lid])
        assert second == []  # уже реактивирован — второй вызов не находит кандидата

        hist_count = await fetchval(
            "SELECT count(*) FROM listing_archive_history WHERE listing_id = $1", lid)
        assert hist_count == 1  # ровно одна запись истории, не задвоена
    finally:
        await _cleanup(lid)


@pytest.mark.asyncio
async def test_reactivate_leaves_still_gone_listing_untouched(db):
    """last_seen НЕ продвинулся мимо archived_at (никто не видел
    объявление снова) — реактивации не происходит, остаётся архивным."""
    from datetime import datetime, timedelta, timezone
    from bot.core.archive_check import reactivate_reappeared_listings
    from bot.db.pg import execute, fetchrow

    lid = "__test_ac_still_gone__"
    now = datetime.now(timezone.utc)
    last_seen = now - timedelta(days=5)   # последний раз видели ДО архивации
    archived_at = now - timedelta(days=2)  # архивировано ПОЗЖЕ last_seen
    await execute("""
        INSERT INTO apartment_listings (id, url, is_active, archived_at, archive_reason, last_seen)
        VALUES ($1, $2, FALSE, $3, 'confirmed_gone', $4)
        ON CONFLICT (id) DO UPDATE SET is_active = FALSE, archived_at = $3,
            archive_reason = 'confirmed_gone', last_seen = $4
    """, lid, f"https://krisha.kz/test/{lid}", archived_at, last_seen)
    try:
        reactivated = await reactivate_reappeared_listings(listing_ids=[lid])
        assert reactivated == []

        row = await fetchrow(
            "SELECT is_active, archived_at, archive_reason FROM apartment_listings WHERE id = $1", lid)
        assert row["is_active"] is False
        assert row["archived_at"] == archived_at
        assert row["archive_reason"] == "confirmed_gone"
    finally:
        await _cleanup(lid)


@pytest.mark.asyncio
async def test_reactivate_scoped_by_listing_ids_ignores_others(db):
    """listing_ids-скоуп — не задевает другие подходящие под условие
    строки (задача: тесты не должны трогать реальный прод-бэклог, 211
    строк на момент написания уже подходят под условие реактивации без
    скоупа)."""
    from datetime import datetime, timedelta, timezone
    from bot.core.archive_check import reactivate_reappeared_listings
    from bot.db.pg import execute, fetchrow

    lid_scoped, lid_other = "__test_ac_scope_a__", "__test_ac_scope_b__"
    archived_at = datetime.now(timezone.utc) - timedelta(days=2)
    last_seen = datetime.now(timezone.utc)
    for lid in (lid_scoped, lid_other):
        await execute("""
            INSERT INTO apartment_listings (id, url, is_active, archived_at, archive_reason, last_seen)
            VALUES ($1, $2, FALSE, $3, 'confirmed_gone', $4)
            ON CONFLICT (id) DO UPDATE SET is_active = FALSE, archived_at = $3,
                archive_reason = 'confirmed_gone', last_seen = $4
        """, lid, f"https://krisha.kz/test/{lid}", archived_at, last_seen)
    try:
        reactivated = await reactivate_reappeared_listings(listing_ids=[lid_scoped])
        assert reactivated == [lid_scoped]

        other = await fetchrow("SELECT is_active FROM apartment_listings WHERE id = $1", lid_other)
        assert other["is_active"] is False  # вне скоупа — не тронут, даже хотя подходил бы
    finally:
        await _cleanup(lid_scoped, lid_other)
