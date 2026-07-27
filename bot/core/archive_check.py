"""
Проверка актуальности объявлений: не ушло ли в архив на krisha.kz.

Проблема: парсер видит только первые страницы выдачи, поэтому проданные /
снятые объявления навсегда остаются "живыми" в БД и висят в топах.

Решение: каждый цикл проверяем страницы N лучших активных объявлений
(давно не проверявшихся) и помечаем архивные is_active=FALSE.

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
    if resp.status_code in (404, 410):
        return "deleted"
    if resp.status_code in (403, 429):
        logger.warning("archive check blocked (%s), stopping", resp.status_code)
        raise RuntimeError("blocked")
    if resp.status_code != 200:
        return None
    text = resp.text
    return "archived" if any(m in text for m in ARCHIVE_MARKERS) else "alive"


async def check_archived(limit: int = 20) -> dict:
    """
    Проверить limit лучших активных объявлений, которые давно не проверялись.
    Возвращает {"checked": n, "archived": n}.
    """
    # БАГ (найден): сортировка по score_total DESC ставила уже проверенные
    # вчера высокобалльные объявления впереди НИКОГДА не проверенных с чуть
    # меньшим скором — при том, что новых объявлений в базу приходит больше,
    # чем ARCHIVE_CHECK_BATCH способен обработать за цикл (единичные секунды
    # на объявление, намеренно медленно), очередь никогда не догоняла саму
    # себя: часть базы (десятки тысяч активных) годами оставалась вообще
    # непроверенной. Теперь сперва ВСЕГДА добираем никогда не проверенные
    # (archive_checked_at IS NULL), и только когда таких не осталось —
    # самые старые по последней проверке; скор — уже вторичный тай-брейк.
    rows = await fetch("""
        SELECT id, url FROM apartment_listings
        WHERE is_active IS NOT FALSE
          AND url IS NOT NULL
          AND (archive_checked_at IS NULL OR archive_checked_at < now() - interval '24 hours')
        ORDER BY archive_checked_at ASC NULLS FIRST, score_total DESC NULLS LAST
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
            if result == "deleted":
                # Страницы больше нет — удаляем и из нашей базы (правило:
                # архив = остаётся в БД / скрыт из Sheets; удалено = удаляем везде)
                archived += 1
                await execute("DELETE FROM apartment_listings WHERE id = $1", r["id"])
                logger.info("deleted (страница удалена): %s", r["url"])
            elif result == "archived":
                archived += 1
                await execute("""
                    UPDATE apartment_listings
                    SET is_active = FALSE, archived_at = now(), archive_checked_at = now()
                    WHERE id = $1
                """, r["id"])
                logger.info("archived: %s", r["url"])
            else:
                await execute(
                    "UPDATE apartment_listings SET archive_checked_at = now() WHERE id = $1",
                    r["id"],
                )
    logger.info("archive check: %d checked, %d archived/deleted", checked, archived)
    return {"checked": checked, "archived": archived}
