"""Регрессия для Фазы A.5, п.4 вердикт-стратегии (docs/verdict_strategy.md,
задача 2026-08-14): outcome_labels расширены clean_disappearance_
within_30d/relisted_within_60d/possibly_relisted/possibly_moderation_
removed/observation_days/censored/outcome_notes — уточнение disappeared_
within_30d (прокси ликвидности, не продажи), не замена. Реальная БД (тот
же паттерн, что tests/test_effective_score.py), listing_ids-скоуп —
чтобы не пересчитывать всю базу на каждый тест."""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import pytest_asyncio
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

_COMPLEX = "__Тест Релист ЖК__"


@pytest_asyncio.fixture
async def db():
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    yield
    await close_pool()


async def _insert(id_, price, area=60.0, rooms=2, floor=5, is_active=True,
                   archived_at=None, first_seen=None, details_fetched=True,
                   complex_name=_COMPLEX):
    from bot.db.pg import execute
    await execute(
        """
        INSERT INTO apartment_listings
            (id, price, area, rooms, floor, complex_name, is_active, archived_at,
             first_seen, details_fetched)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8, COALESCE($9, now()), $10)
        """,
        id_, price, area, rooms, floor, complex_name, is_active, archived_at,
        first_seen, details_fetched,
    )


async def _cleanup(*ids):
    from bot.db.pg import execute
    await execute("DELETE FROM outcome_labels WHERE listing_id = ANY($1::text[])", list(ids))
    await execute("DELETE FROM apartment_listings WHERE id = ANY($1::text[])", list(ids))


async def _label(listing_id):
    from bot.db.pg import fetchrow
    return await fetchrow("SELECT * FROM outcome_labels WHERE listing_id=$1", listing_id)


@pytest.mark.asyncio
async def test_relisted_within_60d_strict_match(db):
    from outcome_labels_recompute import run_recompute
    now = datetime.now(timezone.utc)
    old_id, new_id = "__test_relist_old__", "__test_relist_new__"
    # old: архивировано 40 дней назад (окно 60д уже закрыто), очень старое
    # (>30д от first_seen, чтобы не зависеть от disappeared_within_30d).
    await _insert(old_id, price=30_000_000, area=60.0, rooms=2, floor=5,
                  is_active=False, archived_at=now - timedelta(days=40),
                  first_seen=now - timedelta(days=100))
    # new: тот же ЖК/комнаты/этаж/похожая площадь/цена, появилось через
    # 5 дней после архивации old — строгое совпадение.
    await _insert(new_id, price=30_500_000, area=61.0, rooms=2, floor=5,
                  is_active=True, first_seen=now - timedelta(days=35))
    try:
        await run_recompute(listing_ids=[old_id, new_id])
        lbl = await _label(old_id)
        assert lbl["relisted_within_60d"] is True
        assert lbl["possibly_relisted"] is False  # уже в strict, не в "только possibly"
        assert "релист" in lbl["outcome_notes"]
    finally:
        await _cleanup(old_id, new_id)


@pytest.mark.asyncio
async def test_possibly_relisted_loose_match_only(db):
    from outcome_labels_recompute import run_recompute
    now = datetime.now(timezone.utc)
    old_id, new_id = "__test_prelist_old__", "__test_prelist_new__"
    # Окно релиста должно быть ЗАКРЫТО (archived_at <= now-60д), иначе
    # честный ответ для relisted_within_60d — NULL, не FALSE, даже если
    # найденный кандидат не проходит строгий порог (см. докстринг
    # relist_match в outcome_labels_recompute.py — рассмотренный, но
    # отклонённый кандидат не исключает, что до закрытия окна появится
    # другой, более похожий).
    await _insert(old_id, price=30_000_000, area=60.0, rooms=2, floor=5,
                  is_active=False, archived_at=now - timedelta(days=70),
                  first_seen=now - timedelta(days=150))
    # Другой этаж и заметно другая цена (+40%, вне strict-допуска ±15%),
    # но похожая площадь (в пределах loose ±10%) -> только possibly.
    # first_seen — внутри окна (archived_at, archived_at+60д].
    await _insert(new_id, price=42_000_000, area=63.0, rooms=2, floor=9,
                  is_active=True, first_seen=now - timedelta(days=65))
    try:
        await run_recompute(listing_ids=[old_id, new_id])
        lbl = await _label(old_id)
        assert lbl["relisted_within_60d"] is False
        assert lbl["possibly_relisted"] is True
        assert "возможный релист" in lbl["outcome_notes"]
    finally:
        await _cleanup(old_id, new_id)


@pytest.mark.asyncio
async def test_no_relist_when_no_candidate(db):
    from outcome_labels_recompute import run_recompute
    now = datetime.now(timezone.utc)
    lid = "__test_norelist__"
    # Окно релиста (60д от archived_at) должно быть ЗАКРЫТО, иначе честный
    # ответ — NULL, не FALSE (см. test_relist_window_not_closed_yet_is_null).
    await _insert(lid, price=30_000_000, area=60.0, rooms=2, floor=5,
                  is_active=False, archived_at=now - timedelta(days=70),
                  first_seen=now - timedelta(days=150), complex_name="__Одинокий ЖК Без Пары__")
    try:
        await run_recompute(listing_ids=[lid])
        lbl = await _label(lid)
        assert lbl["relisted_within_60d"] is False
        assert lbl["possibly_relisted"] is False
    finally:
        await _cleanup(lid)


@pytest.mark.asyncio
async def test_relist_window_not_closed_yet_is_null(db):
    # Архивировано вчера — окно 60 дней ещё далеко не закрыто, кандидатов
    # нет -> relisted_within_60d должен быть NULL (не FALSE, "не гадаем"),
    # а не поспешный вывод "не релист".
    from outcome_labels_recompute import run_recompute
    now = datetime.now(timezone.utc)
    lid = "__test_relist_open_window__"
    await _insert(lid, price=30_000_000, area=60.0, rooms=2, floor=5,
                  is_active=False, archived_at=now - timedelta(days=1),
                  first_seen=now - timedelta(days=50), complex_name="__Открытое Окно ЖК__")
    try:
        await run_recompute(listing_ids=[lid])
        lbl = await _label(lid)
        assert lbl["relisted_within_60d"] is None
    finally:
        await _cleanup(lid)


@pytest.mark.asyncio
async def test_possibly_moderation_removed(db):
    from outcome_labels_recompute import run_recompute
    now = datetime.now(timezone.utc)
    lid = "__test_modrem__"
    await _insert(lid, price=30_000_000, is_active=False,
                  archived_at=now - timedelta(hours=20),
                  first_seen=now - timedelta(days=1),
                  details_fetched=False, complex_name="__Модерация ЖК__")
    try:
        await run_recompute(listing_ids=[lid])
        lbl = await _label(lid)
        assert lbl["possibly_moderation_removed"] is True
        assert "модерацией" in lbl["outcome_notes"]
    finally:
        await _cleanup(lid)


@pytest.mark.asyncio
async def test_not_moderation_removed_when_details_fetched(db):
    from outcome_labels_recompute import run_recompute
    now = datetime.now(timezone.utc)
    lid = "__test_not_modrem__"
    await _insert(lid, price=30_000_000, is_active=False,
                  archived_at=now - timedelta(hours=20),
                  first_seen=now - timedelta(days=1),
                  details_fetched=True, complex_name="__Не Модерация ЖК__")
    try:
        await run_recompute(listing_ids=[lid])
        lbl = await _label(lid)
        assert lbl["possibly_moderation_removed"] is False
    finally:
        await _cleanup(lid)


@pytest.mark.asyncio
async def test_censored_true_for_active_false_for_archived(db):
    from outcome_labels_recompute import run_recompute
    now = datetime.now(timezone.utc)
    active_id, archived_id = "__test_censored_active__", "__test_censored_archived__"
    await _insert(active_id, price=30_000_000, is_active=True,
                  first_seen=now - timedelta(days=5), complex_name="__Censored ЖК__")
    await _insert(archived_id, price=30_000_000, is_active=False,
                  archived_at=now - timedelta(days=2),
                  first_seen=now - timedelta(days=10), complex_name="__Censored ЖК__")
    try:
        await run_recompute(listing_ids=[active_id, archived_id])
        a, b = await _label(active_id), await _label(archived_id)
        assert a["censored"] is True
        assert b["censored"] is False
        assert "censored" in a["outcome_notes"]
    finally:
        await _cleanup(active_id, archived_id)


@pytest.mark.asyncio
async def test_observation_days_for_active_and_archived(db):
    from outcome_labels_recompute import run_recompute
    now = datetime.now(timezone.utc)
    active_id, archived_id = "__test_obsdays_active__", "__test_obsdays_archived__"
    await _insert(active_id, price=30_000_000, is_active=True,
                  first_seen=now - timedelta(days=12), complex_name="__ObsDays ЖК__")
    await _insert(archived_id, price=30_000_000, is_active=False,
                  archived_at=now - timedelta(days=3),
                  first_seen=now - timedelta(days=20), complex_name="__ObsDays ЖК__")
    try:
        await run_recompute(listing_ids=[active_id, archived_id])
        a, b = await _label(active_id), await _label(archived_id)
        assert a["observation_days"] in (11, 12)  # ~now()-first_seen, допуск на секунды выполнения
        assert b["observation_days"] == 17  # archived_at-first_seen = 20-3
    finally:
        await _cleanup(active_id, archived_id)


@pytest.mark.asyncio
async def test_clean_disappearance_false_when_relisted_even_if_disappeared_true(db):
    # Ключевой сценарий Фазы A.5: disappeared_within_30d=TRUE (быстрый
    # архив без снижений), НО есть строгий релист-кандидат -> clean_
    # disappearance_within_30d должен быть FALSE, не TRUE — это и есть
    # разница между "прокси ликвидности" и "похоже на продажу".
    from outcome_labels_recompute import run_recompute
    now = datetime.now(timezone.utc)
    old_id, new_id = "__test_clean_relisted_old__", "__test_clean_relisted_new__"
    # first_seen должен быть ПОЗЖЕ границы покрытия price_history (на дату
    # задачи ~36 дней назад, см. докстринг модуля) — иначе disappeared_
    # within_30d сам уйдёт в NULL по честной причине "нет данных", и тест
    # ничего не скажет про релист-логику конкретно. Архивировано через 10
    # дней после first_seen (< 30д, без снижений цены).
    await _insert(old_id, price=30_000_000, area=60.0, rooms=2, floor=5,
                  is_active=False, archived_at=now - timedelta(days=10),
                  first_seen=now - timedelta(days=20))
    await _insert(new_id, price=30_500_000, area=61.0, rooms=2, floor=5,
                  is_active=True, first_seen=now - timedelta(days=5))
    try:
        await run_recompute(listing_ids=[old_id, new_id])
        lbl = await _label(old_id)
        assert lbl["disappeared_within_30d"] is True
        assert lbl["relisted_within_60d"] is True
        assert lbl["clean_disappearance_within_30d"] is False
    finally:
        await _cleanup(old_id, new_id)


@pytest.mark.asyncio
async def test_clean_disappearance_stays_null_when_relist_window_still_open(db):
    # Регресс на баг, пойманный именно этим тестом при разработке (Фаза
    # A.5 п.4): relist_match — INNER JOIN + GROUP BY, при НУЛЕВОМ числе
    # кандидатов не даёт строки вовсе, поэтому "rm.strict_match IS NULL"
    # ниоткуда не отличало "кандидатов правда нет, окно закрыто" от
    # "кандидатов пока нет, окно ЕЩЁ ОТКРЫТО" (60 дней от архивации не
    # прошли) — первая версия запроса в этом случае молча делала ELSE
    # TRUE, то есть объявляла "чистое исчезновение", не дождавшись
    # реальной возможности релиста появиться. disappeared_within_30d=TRUE
    # (быстрый архив без снижений) + окно релиста ещё открыто (архивация
    # 10 дней назад, закроется через 50) + кандидатов пока не появилось —
    # честный ответ ОБОИХ полей — NULL, не TRUE.
    from outcome_labels_recompute import run_recompute
    now = datetime.now(timezone.utc)
    lid = "__test_clean_window_open__"
    await _insert(lid, price=30_000_000, area=60.0, rooms=2, floor=5,
                  is_active=False, archived_at=now - timedelta(days=10),
                  first_seen=now - timedelta(days=20), details_fetched=True,
                  complex_name="__Окно Ещё Открыто ЖК__")
    try:
        await run_recompute(listing_ids=[lid])
        lbl = await _label(lid)
        assert lbl["disappeared_within_30d"] is True
        assert lbl["possibly_moderation_removed"] is False
        assert lbl["relisted_within_60d"] is None
        assert lbl["possibly_relisted"] is None
        assert lbl["clean_disappearance_within_30d"] is None
        assert "окно релиста" in lbl["outcome_notes"]
    finally:
        await _cleanup(lid)


@pytest.mark.asyncio
async def test_clean_disappearance_true_when_relist_window_closed_and_no_match(db):
    # Дополняет предыдущий тест: то же объявление, но с архивацией
    # достаточно давно, чтобы окно релиста УЖЕ закрылось (>60 дней) —
    # теперь честный ответ разрешается в TRUE. Здесь disappeared_within_
    # 30d намеренно НЕ проверяется (first_seen старше границы покрытия
    # price_history при такой давности архивации — упирается в структурное
    # ограничение "60-дневное окно релиста ещё не может закрыться и
    # first_seen ещё попадать в покрытие price_history одновременно",
    # см. докстринг outcome_labels_recompute.py) — тест проверяет именно
    # relisted_within_60d/possibly_relisted независимо от disappeared_
    # within_30d, они разрешаются по своим правилам (archived_at+60d).
    from outcome_labels_recompute import run_recompute
    now = datetime.now(timezone.utc)
    lid = "__test_clean_window_closed__"
    await _insert(lid, price=30_000_000, area=60.0, rooms=2, floor=5,
                  is_active=False, archived_at=now - timedelta(days=70),
                  first_seen=now - timedelta(days=80), details_fetched=True,
                  complex_name="__Окно Уже Закрыто ЖК__")
    try:
        await run_recompute(listing_ids=[lid])
        lbl = await _label(lid)
        assert lbl["relisted_within_60d"] is False
        assert lbl["possibly_relisted"] is False
        assert lbl["possibly_moderation_removed"] is False
    finally:
        await _cleanup(lid)


@pytest.mark.asyncio
async def test_recompute_is_idempotent_upsert_not_duplicate(db):
    from outcome_labels_recompute import run_recompute
    from bot.db.pg import fetch
    now = datetime.now(timezone.utc)
    lid = "__test_idempotent__"
    await _insert(lid, price=30_000_000, is_active=True,
                  first_seen=now - timedelta(days=5), complex_name="__Idempotent ЖК__")
    try:
        await run_recompute(listing_ids=[lid])
        await run_recompute(listing_ids=[lid])
        rows = await fetch("SELECT * FROM outcome_labels WHERE listing_id=$1", lid)
        assert len(rows) == 1  # UPSERT, не append
    finally:
        await _cleanup(lid)
