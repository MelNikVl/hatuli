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


# Сколько кандидатов на реактивацию реально ПРОВЕРЯЕМ по сети за один
# вызов verify_reactivation_candidates() (задача 2026-08-17, "safe
# reactivation" follow-up) — НЕ растягиваем бюджет запросов check_archived()
# ради этого: реальный HTTP-чек, та же нагрузка, что и обычная архивная
# проверка ниже, поэтому небольшая отдельная константа, не "все сразу".
_REACTIVATION_CHECK_BATCH = 10


async def _select_reactivation_candidates(limit: int, listing_ids: list[str] | None) -> list[dict]:
    """Кандидаты "last_seen ушёл вперёд archived_at" — ТОЛЬКО кандидаты
    на проверку (задача, явно, п.1: "last_seen > archived_at только
    добавляет объявление в очередь повторной проверки"). is_active тут
    НЕ трогается вообще — это чистое SELECT. ORDER BY archive_checked_at
    ASC NULLS FIRST — справедливая ротация: непроверенные и давно не
    проверявшиеся идут первыми, не одни и те же строки на каждый вызов."""
    from bot.db.pg import fetch
    sql = ("SELECT id, url, archived_at, archive_reason FROM apartment_listings "
           "WHERE is_active = FALSE AND archived_at IS NOT NULL "
           "AND last_seen IS NOT NULL AND last_seen > archived_at")
    params: list = []
    if listing_ids is not None:
        params.append(listing_ids)
        sql += f" AND id = ANY(${len(params)}::text[])"
    params.append(limit)
    sql += f" ORDER BY archive_checked_at ASC NULLS FIRST LIMIT ${len(params)}"
    rows = await fetch(sql, *params)
    return [dict(r) for r in rows]


async def _confirm_reactivation(listing_id: str) -> bool:
    """Единственное место, где is_active реально становится TRUE —
    ОДНИМ атомарным CTE (SELECT старых archived_at/archive_reason + INSERT
    в listing_archive_history + UPDATE apartment_listings), тот же приём,
    что был раньше на всю пачку разом — здесь на одну ПОДТВЕРЖДЁННУЮ
    строку, вызывается ТОЛЬКО после реального 'alive' с Крыши (задача,
    п.4: "если объявление действительно снова доступно"). Возвращает
    False, если строка уже не подходит под условие (кто-то её тронул
    между SELECT-кандидатов и этим вызовом, или уже реактивирована
    параллельным вызовом — идемпотентность и здесь, не только на уровне
    всей функции)."""
    from bot.db.pg import fetchrow
    row = await fetchrow("""
        WITH candidate AS (
            SELECT id, archived_at, archive_reason FROM apartment_listings
            WHERE id = $1 AND is_active = FALSE AND archived_at IS NOT NULL
              AND last_seen IS NOT NULL AND last_seen > archived_at
            FOR UPDATE
        ), logged AS (
            INSERT INTO listing_archive_history (listing_id, archived_at, archive_reason)
            SELECT id, archived_at, archive_reason FROM candidate
            RETURNING listing_id
        )
        UPDATE apartment_listings
        SET is_active = TRUE, archived_at = NULL, archive_reason = NULL, archive_checked_at = NULL
        WHERE id = (SELECT listing_id FROM logged)
        RETURNING id
    """, listing_id)
    return row is not None


async def verify_reactivation_candidates(limit: int = _REACTIVATION_CHECK_BATCH,
                                          listing_ids: list[str] | None = None) -> dict:
    """Задача 2026-08-17, "safe reactivation — только после фактической
    проверки". ЗАМЕНЯЕТ прежнюю reactivate_reappeared_listings() (которая
    ставила is_active=TRUE СРАЗУ по одному условию last_seen > archived_at
    — read-only аудит, scripts/audit_reactivation_candidates.py, нашёл
    50% ложных срабатываний из 20 реально проверенных примеров: страница
    ВСЁ ЕЩЁ archived/404 у половины). Новый процесс, ровно как в задаче:
      1. last_seen > archived_at — ТОЛЬКО кандидат на проверку
         (_select_reactivation_candidates), is_active НЕ трогается;
      2. до проверки is_active остаётся FALSE — ложный кандидат НИ НА
         МГНОВЕНИЕ не появляется в активной выдаче (нет отдельного шага
         "поставить TRUE, потом откатить" — is_active меняется РОВНО
         ОДИН РАЗ, ВНУТРИ _confirm_reactivation, и только на 'alive');
      3. проверка — ТОТ ЖЕ _check_one/HEADERS, что использует check_
         archived() ниже (не вторая копия HTTP-логики);
      4. 'alive' -> _confirm_reactivation(): атомарно история +
         is_active=TRUE + archived_at/archive_reason/archive_checked_at
         очищены;
      5. 'archived'/'deleted' (страница подтверждает, что ОБЪЯВЛЕНИЯ ВСЁ
         ЕЩЁ нет) -> is_active остаётся FALSE, ТОЛЬКО archive_checked_at
         = now() (задача, явно: "не записывать ложную реактивацию в
         историю") — archived_at/archive_reason НЕ трогаются, ORDER BY
         archive_checked_at ASC в следующем вызове эту строку отодвигает
         в конец очереди (не долбим один и тот же ложный кандидат на
         каждом цикле, честная ротация по всем 211+).
      None ('alive' не определился — сеть/несетевая ошибка _check_one)
        -> ничего не меняем вообще, строка остаётся кандидатом со старым
        archive_checked_at, попробуем снова в следующий раз.
      RuntimeError (заблокировали 403/429) -> прерываем батч сразу же,
        тот же принцип, что check_archived() ниже.

    last_seen НЕ трогается нигде в этой функции (задача, явно).

    Идемпотентность: и на уровне выборки (_select_reactivation_candidates
    не находит уже реактивированную is_active=TRUE строку повторно), и на
    уровне самого _confirm_reactivation (WHERE внутри CTE проверяет
    условие ЗАНОВО на момент своего выполнения — гонка с параллельным
    вызовом даёт False, не двойную реактивацию/двойную запись в историю).

    Вызывается в начале check_archived() с небольшим _REACTIVATION_CHECK_
    BATCH — реальные HTTP-запросы, тот же бюджет цикла, что и обычная
    архивная проверка, не отдельный неограниченный проход по всем 213.

    ПРОВЕРЕНО на реальных данных: 213 строк на проде прямо сейчас
    подходят как КАНДИДАТЫ (не как "уже реактивированные" — ни одна не
    трогается, пока функция ФАКТИЧЕСКИ не проверит её по HTTP). Не
    запускалась вручную на всю таблицу в рамках этой задачи.

    listing_ids — опциональный скоуп ТОЛЬКО для тестов (тот же приём, что
    bot/jobs/property_identity_incremental.py::_fetch_unlinked)."""
    from bot.db.pg import execute

    candidates = await _select_reactivation_candidates(limit, listing_ids)
    checked_ids: list[str] = []
    reactivated_ids: list[str] = []
    remained_archived_ids: list[str] = []

    async with httpx.AsyncClient(headers=HEADERS, timeout=25.0, follow_redirects=True) as client:
        for c in candidates:
            await asyncio.sleep(random.uniform(2.5, 5.0))
            try:
                result = await _check_one(client, c["url"])
            except RuntimeError:
                logger.warning("verify_reactivation_candidates: заблокировали, останавливаю батч")
                break
            if result is None:
                continue  # сеть подвела — не трогаем строку, попробуем в другой раз

            checked_ids.append(c["id"])
            if result == "alive":
                if await _confirm_reactivation(c["id"]):
                    reactivated_ids.append(c["id"])
                # else: кто-то другой уже реактивировал/тронул строку
                # между SELECT и этим моментом — идемпотентность, не
                # ошибка, просто не наш реактивированный список.
            else:  # 'archived' | 'deleted' — подтверждённо всё ещё недоступно
                await execute(
                    "UPDATE apartment_listings SET archive_checked_at = now() WHERE id = $1",
                    c["id"])
                remained_archived_ids.append(c["id"])

    # Задача, явно: "добавь в лог количество... и ID проверенных/
    # реактивированных/оставшихся архивными".
    logger.info(
        "verify_reactivation_candidates: checked=%d ids=%s reactivated=%d ids=%s "
        "remained_archived=%d ids=%s",
        len(checked_ids), checked_ids, len(reactivated_ids), reactivated_ids,
        len(remained_archived_ids), remained_archived_ids,
    )
    return {
        "checked": len(checked_ids), "checked_ids": checked_ids,
        "reactivated": len(reactivated_ids), "reactivated_ids": reactivated_ids,
        "remained_archived": len(remained_archived_ids), "remained_archived_ids": remained_archived_ids,
    }


async def check_archived(limit: int = 20) -> dict:
    """
    Проверить до limit объявлений по приоритету hot -> cold-confirm ->
    backlog (см. докстринг модуля, _select_candidates). В начале —
    verify_reactivation_candidates() (см. её докстринг — ТОЛЬКО после
    реальной HTTP-проверки, не по одному last_seen > archived_at).
    Возвращает {"checked", "archived", "hot_checked", "cold_confirm_checked",
    "backlog_checked", "reactivated", "reactivation_checked",
    "reactivation_remained_archived"}.
    """
    reactivation_report = await verify_reactivation_candidates()
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
                # archived_at = COALESCE(archived_at, now()) — задача, явно:
                # повторный confirmed-404/badge на УЖЕ архивной строке не
                # должен двигать archived_at вперёд (в норме такая строка и
                # не должна попадать в _select_candidates заново, WHERE
                # is_active IS NOT FALSE её исключает — но UPDATE защищается
                # сам по себе, а не полагается только на выборку выше; и
                # если строку когда-нибудь передадут сюда напрямую — первая
                # дата архивации остаётся первой). archive_checked_at
                # ("когда последний раз проверяли") ОБНОВЛЯЕТСЯ всегда — это
                # не то же самое, что archived_at ("когда архивировали
                # впервые"). last_seen эта функция не трогает вообще (не в
                # SET) — обновляет его ТОЛЬКО парсер при повторном появлении
                # объявления в выдаче, см. verify_reactivation_candidates().
                await execute("""
                    UPDATE apartment_listings
                    SET is_active = FALSE, archived_at = COALESCE(archived_at, now()),
                        archive_checked_at = now(), archive_reason = $2
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
              "backlog_checked": pool_counts["backlog"],
              "reactivated": reactivation_report["reactivated"],
              "reactivation_checked": reactivation_report["checked"],
              "reactivation_remained_archived": reactivation_report["remained_archived"]}
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
                # Тот же archived_at = COALESCE(...) идемпотентности фикс,
                # что check_archived() выше — та же причина.
                await execute("""
                    UPDATE rental_listings
                    SET is_active = FALSE, archived_at = COALESCE(archived_at, now()),
                        archive_checked_at = now(), archive_reason = $2
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
