"""
Проверка актуальности объявлений: не ушло ли в архив на krisha.kz.

Проблема: парсер видит только первые страницы выдачи, поэтому проданные /
снятые объявления навсегда остаются "живыми" в БД и висят в топах.

Решение (задача "adaptive recheck", 2026-08-13, см. docs/adaptive_recheck_plan.md):
раньше — единая FIFO-очередь по archive_checked_at на ВСЕ активные
объявления (~42к) — на текущем бюджете (~40/цикл) круг занимал ~48
суток, несовместимо с целью "круг <24ч". Круг <24ч для ВСЕХ физически
не влезает в детальную точечную проверку (1 запрос = 1 объявление) без
роста темпа — расчёт в docs/adaptive_recheck_plan.md. Теперь — два
уровня:
  - **hot** (score_total >= HOT_SCORE_THRESHOLD, ~0.4% активных) —
    дорогая точечная проверка на регулярной основе (цель 6-12ч, факт
    ~5.4ч при текущем размере hot-пула и ARCHIVE_CHECK_BATCH).
  - **cold** — дешёвый сигнал "пропало из каталога" (deep sweep,
    service_apartments.py, круг <24ч, отдельная метрика
    DEEP_SWEEP_CIRCLE_*): `last_seen` не обновлялся с начала текущего
    круга = кандидат. Точечная проверка тут — только ПОДТВЕРЖДЕНИЕ
    (защита от шума ре-ранжирования Крыши — "не попал в срез страниц
    в этом круге" не значит "точно архив"), не основной метод.
  - **backlog** — страховка на случай, если круга каталога ещё не было
    (первый запуск) или hot+cold-confirm не выбрали весь бюджет цикла.

Признаки архива на странице krisha: бейдж "В архиве" / "Объявление может
быть неактуальным", либо 404/410.
"""
from __future__ import annotations

import asyncio
import logging
import random

import httpx

from bot.db.pg import execute, fetch

logger = logging.getLogger(__name__)

# Реальные HTTP-запросы archive_check к Крыше за цикл — тот же паттерн,
# что apartment_parser.REQUEST_COUNTS/apartment_details.REQUEST_COUNTS,
# нужен для честного "запросы/сутки" в parser_cycle_history (раньше эта
# нагрузка не считалась вовсе — свой httpx.AsyncClient, отдельный от
# apartment_parser/apartment_details).
REQUEST_COUNTS = {"archive_check": 0}

# Последний результат check_archived() (пулы hot/cold_confirm/backlog) —
# check_archived() зовётся ИЗНУТРИ service_apartments.run_cycle(), его
# возврат туда же и остаётся; _run_cycle_timed() снимает parser_cycle_
# history ПОСЛЕ run_cycle() целиком и не видит промежуточный return —
# тот же паттерн, что REQUEST_COUNTS (модульная переменная, читается
# снаружи после вызова).
LAST_CHECK_RESULT: dict = {}

# Порог "hot" — score_total, выше которого объявление проверяется часто
# (см. docs/adaptive_recheck_plan.md, п.3 — score_total, не новизна:
# гипотеза "hot=новое" проверена на факте и отвергнута, 58% архиваций
# случается у объявлений старше 14 дней). Триггер пересмотра порога:
# медианный лаг детекта в hot-пуле систематически >12ч — см. дашборд
# /admin/parsers?tab=recheck.
HOT_SCORE_THRESHOLD = 90

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9",
}

ARCHIVE_MARKERS = (
    "В архиве",
    "может быть неактуальным",
    "Объявление не найдено",
    "объявление удалено",
)


async def _check_one(client: httpx.AsyncClient, url: str) -> str | None:
    """'deleted' = страницы больше нет (404/410), 'archived' = помечено
    архивом на странице, 'alive' = живое, None = не удалось проверить."""
    try:
        resp = await client.get(url)
    except Exception as exc:
        logger.warning("archive check failed %s: %s", url, exc)
        return None
    REQUEST_COUNTS["archive_check"] += 1
    if resp.status_code in (404, 410):
        return "deleted"
    if resp.status_code in (403, 429):
        logger.warning("archive check blocked (%s), stopping", resp.status_code)
        raise RuntimeError("blocked")
    if resp.status_code != 200:
        return None
    text = resp.text
    return "archived" if any(m in text for m in ARCHIVE_MARKERS) else "alive"


async def _select_candidates(limit: int) -> list[dict]:
    """Приоритет hot -> cold-confirm -> backlog (см. докстринг модуля).
    Три отдельных запроса вместо одного ORDER BY с CASE — читается и
    логируется по пулам отдельно, что и нужно для дашборда/лаг-метрик.
    ИСТОРИЯ: до 2026-08-13 тут была одна FIFO-очередь по всем активным
    (archive_checked_at ASC NULLS FIRST, score_total как вторичный
    тай-брейк) — на бюджете ~40/цикл круг занимал ~48 суток на 42к
    активных, несовместимо с целью "круг <24ч" (см.
    docs/adaptive_recheck_plan.md). Убрана целиком, не расширена —
    рудимент архитектуры "проверяем всех одинаково часто", не
    совместимый с hot/cold-разделением."""
    hot = await fetch("""
        SELECT id, url, 'hot' AS pool FROM apartment_listings
        WHERE is_active IS NOT FALSE AND url IS NOT NULL AND score_total >= $2
        ORDER BY archive_checked_at ASC NULLS FIRST
        LIMIT $1
    """, limit, HOT_SCORE_THRESHOLD)
    candidates = list(hot)

    remaining = limit - len(candidates)
    if remaining > 0:
        from datetime import datetime
        from bot.db import settings as app_settings
        await app_settings.load()
        circle_started_raw = app_settings.get("DEEP_SWEEP_CIRCLE_STARTED_AT")
        circle_started_at = None
        if circle_started_raw:
            try:
                circle_started_at = datetime.fromisoformat(circle_started_raw)
            except ValueError:
                logger.warning("DEEP_SWEEP_CIRCLE_STARTED_AT не парсится: %r", circle_started_raw)
        if circle_started_at:
            cold_confirm = await fetch("""
                SELECT id, url, 'cold_confirm' AS pool FROM apartment_listings
                WHERE is_active IS NOT FALSE AND url IS NOT NULL AND score_total < $3
                  AND last_seen < $2
                  AND (archive_checked_at IS NULL OR archive_checked_at < $2)
                ORDER BY last_seen ASC NULLS FIRST
                LIMIT $1
            """, remaining, circle_started_at, HOT_SCORE_THRESHOLD)
            candidates += list(cold_confirm)

    remaining = limit - len(candidates)
    if remaining > 0:
        # БАГ (найден 2026-08-16 тестом на пустой/малонаселённой БД —
        # test_cold_confirm_only_for_listings_missed_this_circle, на
        # реальных 42к+ строк маскировался объёмом данных): backlog ниже
        # отбирает по archive_checked_at IS NULL — тому же признаку, что
        # и cold_confirm выше (тот тоже допускает archive_checked_at IS
        # NULL через OR). Без исключения id, уже отобранных cold_confirm,
        # одна и та же строка попадала в candidates ДВАЖДЫ — с меткой
        # 'cold_confirm', следом с 'backlog' (бюджет цикла тратился на
        # неё дважды, а вызывающий код, строящий {id: pool}, молча терял
        # первую запись). "id != ALL(...)" ниже исключает уже отобранные
        # hot/cold_confirm id из выборки backlog.
        candidate_ids = [c["id"] for c in candidates]
        # Страховка: круга каталога ещё не было (первый запуск после
        # деплоя) ИЛИ hot+cold-confirm не выбрали весь бюджет цикла —
        # добираем никогда не проверенных (старый бэклог, 34983 на
        # момент расчёта), чтобы бюджет не простаивал впустую.
        backlog = await fetch("""
            SELECT id, url, 'backlog' AS pool FROM apartment_listings
            WHERE is_active IS NOT FALSE AND url IS NOT NULL AND score_total < $2
              AND archive_checked_at IS NULL
              AND id != ALL($3::text[])
            ORDER BY first_seen ASC NULLS FIRST
            LIMIT $1
        """, remaining, HOT_SCORE_THRESHOLD, candidate_ids)
        candidates += list(backlog)

    return candidates


async def check_archived(limit: int = 20) -> dict:
    """
    Проверить до limit объявлений по приоритету hot -> cold-confirm ->
    backlog (см. докстринг модуля, _select_candidates).
    Возвращает {"checked", "archived", "hot_checked", "cold_confirm_checked",
    "backlog_checked"}.
    """
    rows = await _select_candidates(limit)
    pool_counts = {"hot": 0, "cold_confirm": 0, "backlog": 0}

    checked = archived = 0
    async with httpx.AsyncClient(headers=HEADERS, timeout=25.0, follow_redirects=True) as client:
        for r in rows:
            await asyncio.sleep(random.uniform(2.5, 5.0))
            try:
                result = await _check_one(client, r["url"])
            except RuntimeError:
                break  # заблокировали — не продолжаем в этом цикле
            if result is None:
                continue
            checked += 1
            pool_counts[r["pool"]] = pool_counts.get(r["pool"], 0) + 1
            if result in ("deleted", "archived"):
                # НАЙДЕНО (задача 2026-08-17, scripts/audit_orphan_properties.py,
                # коммит 181636e): DELETE FROM apartment_listings на "deleted"
                # каскадом убирал property_listings (FK ON DELETE CASCADE), но
                # НЕ properties (родитель) — 15/17 осиротевших properties на
                # проде согласовывались именно с этим. property_listings,
                # price timeline, true DOM — вся история, завязанная на
                # listing_id, терялась НАВСЕГДА в момент DELETE, без recovery.
                # Правило теперь: "объявления больше нет" (404/410) и "Krisha
                # сама пометила архивом" — ОДНО и то же действие с нашей
                # стороны (мягкая архивация, ничего физически не удаляется),
                # различаются только archive_reason (migrations/089) — для
                # отчётности/дебага, не для поведения.
                archived += 1
                archive_reason = "confirmed_gone" if result == "deleted" else "archived_badge"
                await execute("""
                    UPDATE apartment_listings
                    SET is_active = FALSE, archived_at = now(), archive_checked_at = now(),
                        archive_reason = $2
                    WHERE id = $1
                """, r["id"], archive_reason)
                logger.info("archived (%s, pool=%s): %s", archive_reason, r["pool"], r["url"])
            else:
                await execute(
                    "UPDATE apartment_listings SET archive_checked_at = now() WHERE id = $1",
                    r["id"],
                )
    logger.info("archive check: %d checked, %d archived/deleted (hot=%d cold_confirm=%d backlog=%d)",
                checked, archived, pool_counts["hot"], pool_counts["cold_confirm"], pool_counts["backlog"])
    result = {"checked": checked, "archived": archived,
              "hot_checked": pool_counts["hot"], "cold_confirm_checked": pool_counts["cold_confirm"],
              "backlog_checked": pool_counts["backlog"]}
    LAST_CHECK_RESULT.clear()
    LAST_CHECK_RESULT.update(result)
    return result


async def check_archived_rentals(limit: int = 20) -> dict:
    """Аналог check_archived, но для rental_listings — до миграции 040 у
    аренды не было понятия "в архиве" вообще: пропавшее объявление просто
    переставало обновлять last_seen. Нужно для тепловой карты аренды за
    последний месяц (см. archived-rental-points): без этой проверки "ушло
    в архив недавно" — не отличить от "просто давно не перепарсивали"."""
    rows = await fetch("""
        SELECT id, url FROM rental_listings
        WHERE is_active IS NOT FALSE
          AND url IS NOT NULL
          AND (archive_checked_at IS NULL OR archive_checked_at < now() - interval '24 hours')
        ORDER BY archive_checked_at ASC NULLS FIRST, last_seen DESC NULLS LAST
        LIMIT $1
    """, limit)

    checked = archived = 0
    async with httpx.AsyncClient(headers=HEADERS, timeout=25.0, follow_redirects=True) as client:
        for r in rows:
            await asyncio.sleep(random.uniform(2.5, 5.0))
            try:
                result = await _check_one(client, r["url"])
            except RuntimeError:
                break  # заблокировали — не продолжаем в этом цикле
            if result is None:
                continue
            checked += 1
            if result in ("deleted", "archived"):
                # Тот же фикс, что check_archived() выше (задача 2026-08-17,
                # "никаких физических DELETE — только архивирование, вся
                # история сохраняется") — тот же класс потери истории
                # возможен и здесь, хотя у rental_listings нет property_
                # listings, принцип "не удалять безвозвратно" тот же.
                archived += 1
                archive_reason = "confirmed_gone" if result == "deleted" else "archived_badge"
                await execute("""
                    UPDATE rental_listings
                    SET is_active = FALSE, archived_at = now(), archive_checked_at = now(),
                        archive_reason = $2
                    WHERE id = $1
                """, r["id"], archive_reason)
                logger.info("rental archived (%s): %s", archive_reason, r["url"])
            else:
                await execute(
                    "UPDATE rental_listings SET archive_checked_at = now() WHERE id = $1",
                    r["id"],
                )
    logger.info("rental archive check: %d checked, %d archived/deleted", checked, archived)
    return {"checked": checked, "archived": archived}
