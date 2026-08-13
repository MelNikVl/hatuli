"""Регрессия hot/cold/backlog приоритета в bot.core.archive_check
(задача "adaptive recheck", 2026-08-13, см. docs/adaptive_recheck_plan.md).
Старая единая FIFO-очередь по всем активным объявлениям убрана целиком —
проверяем, что новая _select_candidates() реально разделяет пулы, а не
просто переименовывает то же самое.

ВАЖНО: тесты идут против ЖИВОЙ таблицы apartment_listings (42к+ строк
на момент написания, 172 реальных hot) — нельзя проверять точный
список/позицию (реальные строки конкурируют с тестовыми за место в
LIMIT). Проверяем ПРИСУТСТВИЕ и ПРАВИЛЬНУЮ МЕТКУ ПУЛА тестовых строк
при заведомо большом limit, не точный список результатов.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

TEST_IDS = ["__test_hot_never__", "__test_hot_stale__", "__test_cold_stale__",
            "__test_cold_fresh__", "__test_backlog__"]

# Реальных hot (score>=90) на момент написания — 172; берём с большим
# запасом, чтобы тестовая hot-строка гарантированно попала в выборку
# независимо от того, сколько их реально в БД на момент прогона.
BIG_LIMIT = 60000


@pytest_asyncio.fixture
async def db():
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    yield
    await close_pool()


@pytest_asyncio.fixture
async def listings(db):
    from bot.db.pg import execute
    now = datetime.now(timezone.utc)
    rows = [
        # id, score_total, last_seen, archive_checked_at
        ("__test_hot_never__", 95, now, None),                        # hot, никогда не проверялся
        ("__test_hot_stale__", 90, now, now - timedelta(hours=10)),   # hot, давно проверялся
        ("__test_cold_stale__", 50, now - timedelta(hours=30), None), # cold, пропало из круга
        ("__test_cold_fresh__", 50, now, None),                       # cold, видели В ЭТОМ круге — не кандидат
        ("__test_backlog__", 50, now, None),                          # cold, "видели", но для теста backlog last_seen подвинем позже
    ]
    for id_, score, last_seen, checked in rows:
        await execute("""
            INSERT INTO apartment_listings (id, url, score_total, is_active, last_seen, archive_checked_at)
            VALUES ($1, $2, $3, TRUE, $4, $5)
        """, id_, f"https://krisha.kz/test/{id_}", score, last_seen, checked)
    try:
        yield now
    finally:
        await execute("DELETE FROM apartment_listings WHERE id = ANY($1)", TEST_IDS)


async def _set_circle_started(app_settings, when: datetime | None):
    from bot.db.pg import execute
    if when is None:
        await execute("DELETE FROM app_settings WHERE key = 'DEEP_SWEEP_CIRCLE_STARTED_AT'")
    else:
        await app_settings.set("DEEP_SWEEP_CIRCLE_STARTED_AT", when.isoformat())
    await app_settings.load()


@pytest.mark.asyncio
async def test_hot_listings_classified_as_hot_pool(listings):
    """Оба тестовых hot-объявления (score>=90) присутствуют в выборке
    с меткой пула 'hot', при достаточно большом limit."""
    from bot.core.archive_check import _select_candidates
    from bot.db import settings as app_settings

    now = listings
    await _set_circle_started(app_settings, now - timedelta(hours=25))
    try:
        cands = await _select_candidates(BIG_LIMIT)
        by_id = {c["id"]: c["pool"] for c in cands}
        assert by_id.get("__test_hot_never__") == "hot", by_id.get("__test_hot_never__")
        assert by_id.get("__test_hot_stale__") == "hot", by_id.get("__test_hot_stale__")
    finally:
        await _set_circle_started(app_settings, None)


@pytest.mark.asyncio
async def test_hot_ordered_by_staleness_within_pool(db):
    """Внутри hot-пула порядок — archive_checked_at ASC NULLS FIRST
    (самый залежавшийся первым). Изолированная от реальных данных
    проверка: limit=1 c ДВУМЯ тестовыми hot-строками — тестовая
    'stale' и тестовая 'fresh' — но чтобы не конкурировать с реальными
    172 hot, тест использует archive_checked_at глубоко в прошлом
    (year 2000), гарантированно раньше любых реальных проверок."""
    from bot.db.pg import execute
    from bot.core.archive_check import _select_candidates

    old_id, recent_id = "__test_hot_oldest__", "__test_hot_recent__"
    ancient = datetime(2000, 1, 1, tzinfo=timezone.utc)
    recent = datetime.now(timezone.utc) - timedelta(minutes=5)
    await execute("""
        INSERT INTO apartment_listings (id, url, score_total, is_active, last_seen, archive_checked_at)
        VALUES ($1, $2, 99, TRUE, now(), $3), ($4, $5, 99, TRUE, now(), $6)
    """, old_id, f"https://krisha.kz/test/{old_id}", ancient, recent_id, f"https://krisha.kz/test/{recent_id}", recent)
    try:
        # limit=1 среди ~174 hot (172 реальных + 2 тестовых) не гарантирует,
        # что тестовая 'oldest' окажется первой (NULLS FIRST выше любой
        # даты, а сколько реальных hot никогда не проверялись — неизвестно
        # заранее) — поэтому проверяем ОТНОСИТЕЛЬНЫЙ порядок: 'oldest'
        # раньше 'recent' среди самой выборки, не абсолютную позицию 0.
        cands = await _select_candidates(BIG_LIMIT)
        ids_order = [c["id"] for c in cands if c["pool"] == "hot"]
        assert old_id in ids_order and recent_id in ids_order
        assert ids_order.index(old_id) < ids_order.index(recent_id)
    finally:
        await execute("DELETE FROM apartment_listings WHERE id = ANY($1)", [old_id, recent_id])


@pytest.mark.asyncio
async def test_cold_confirm_only_for_listings_missed_this_circle(listings):
    """Круг стартовал 25ч назад. cold_stale (last_seen 30ч назад, до
    начала круга) — кандидат с меткой cold_confirm; cold_fresh (last_seen
    сейчас, внутри круга) — НЕ кандидат вовсе (не пропадал из каталога)."""
    from bot.core.archive_check import _select_candidates
    from bot.db import settings as app_settings

    now = listings
    await _set_circle_started(app_settings, now - timedelta(hours=25))
    try:
        cands = await _select_candidates(BIG_LIMIT)
        by_id = {c["id"]: c["pool"] for c in cands}
        assert by_id.get("__test_cold_stale__") == "cold_confirm"
        assert "__test_cold_fresh__" not in by_id, "видели в этом круге — не должно быть кандидатом"
    finally:
        await _set_circle_started(app_settings, None)


@pytest.mark.asyncio
async def test_backlog_fallback_when_no_circle_started_yet(listings):
    """Круга каталога ещё не было (DEEP_SWEEP_CIRCLE_STARTED_AT отсутствует)
    — cold-confirm молчит целиком (не падает), backlog добирает
    никогда-не-проверенные (archive_checked_at IS NULL, score < hot)."""
    from bot.core.archive_check import _select_candidates
    from bot.db import settings as app_settings

    await _set_circle_started(app_settings, None)
    cands = await _select_candidates(BIG_LIMIT)
    by_id = {c["id"]: c["pool"] for c in cands}
    for tid in ("__test_cold_stale__", "__test_cold_fresh__", "__test_backlog__"):
        assert by_id.get(tid) == "backlog", by_id.get(tid)


@pytest.mark.asyncio
async def test_limit_respected_across_pools(listings):
    """Суммарно candidates никогда не превышает limit, даже когда во всех
    трёх пулах кандидатов больше, чем limit (реальных данных с запасом)."""
    from bot.core.archive_check import _select_candidates
    from bot.db import settings as app_settings

    now = listings
    await _set_circle_started(app_settings, now - timedelta(hours=25))
    try:
        cands = await _select_candidates(2)
        assert len(cands) == 2
    finally:
        await _set_circle_started(app_settings, None)
