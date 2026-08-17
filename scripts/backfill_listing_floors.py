#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/backfill_listing_floors.py — задача 2026-08-17, "Missing floor +
orphan audit" (прямое продолжение Property Identity: production сейчас в
match_mode="candidate_only", следующий необходимый шаг — evidence по
фотографиям и ручная проверка кандидатов, но 1600 apartment_listings без
floor И без property_listings не могут даже попасть в кандидаты вовсе —
bot.identity.property_linker.compute_address_hash() отказывается линковать
без floor, см. её докстринг: "Unknown ≠ average, не гадаем").

## Что именно обрабатывается

ТОЛЬКО apartment_listings, у которых ОДНОВРЕМЕННО:
  - floor IS NULL;
  - НЕТ строки в property_listings (NOT EXISTS).
Listing с floor IS NULL, но УЖЕ связанный (редкий случай — 4 из 1604 на
проде, см. отчёт задачи) — НЕ трогаем: он уже прошёл через Property
Identity с каким-то историческим решением, backfill'ить его floor задним
числом значило бы менять данные под уже принятым (пусть и неполным)
решением, не входит в эту задачу.

## Переиспользуемый механизм (не новый парсер)

bot.core.apartment_details.fetch_apartment_details(url, raise_on_error=True)
— ТА ЖЕ функция, что использует bot/core/coord_backfill.py и живой парсер
(bot/core/apartment_parser.py). raise_on_error=True — новый, обратно-
совместимый (default False) параметр той же функции (см. её докстринг):
поднимает ListingBlockedError на 403/429 вместо тихого {}, пробрасывает
сетевые исключения вместо их проглатывания — нужно ТОЛЬКО этому скрипту
для честной статистики blocked/errors, ни один существующий вызывающий
код это не затрагивает (default не меняется).

## Что пишем, что нет

Обновляем ТОЛЬКО floor (и floors_total — тот же сигнал с той же страницы,
тот же regex, что и floor: "%d из %d"). ВСЕ остальные поля, которые
fetch_apartment_details() тоже возвращает (photos/description/seller_name/
...), сознательно ИГНОРИРУЮТСЯ здесь — задача явно: "не перезаписывать
другие заполненные поля". UPDATE защищён WHERE floor IS NULL — на случай,
если параллельно (coord_backfill.py, живой парсер) тот же listing УЖЕ
получил floor между нашим SELECT и UPDATE, не перезаписываем его чужим
результатом того же самого поля.

## Статистика

found — сколько listing'ов отобрано в этом прогоне (SELECT).
floor_filled — floor успешно распарсен со страницы и записан.
floor_not_found — страница успешно скачана, но floor на ней не нашёлся
  (устаревшая вёрстка / реально не указан продавцом).
unavailable — страница отвечает, но объявление помечено архивным/снятым
  (fetch_apartment_details().get("is_archived") — тот же маркер, что
  bot/core/archive_check.py использует для "В архиве"/"может быть
  неактуальным"). Floor с такой страницы НЕ читаем: архивная страница
  Крыши иногда возвращает урезанную/устаревшую вёрстку, надёжность ниже,
  а листинг всё равно на очереди у archive_check.py на удаление.
blocked — ListingBlockedError (403/429).
errors — любое другое исключение (timeout, DNS, парсинг упал).

## Retry/backoff и ограничение запросов

fetch_apartment_details() уже спит random(3.0, 6.0) сек ПЕРЕД каждым своим
запросом (та же защита, что у coord_backfill.py/apartment_parser.py) — этот
скрипт поверх неё держит ещё один more conservative intra-batch delay
(_DELAY_RANGE, тот же диапазон 8-15с, что coord_backfill.py — целевой
бэклог отдельный и объёмный, 1600 объявлений, нет причины бить чаще, чем
уже проверенный на проде код). Retry — до _MAX_RETRIES на generic-ошибку с
экспоненциальным backoff (_RETRY_DELAYS); ListingBlockedError ретраится
ЗНАЧИТЕЛЬНО реже и медленнее (_BLOCK_RETRY_DELAYS) — повторный немедленный
запрос в блок только тратит бюджет и повышает риск более долгой блокировки.
Исчерпав retries — засчитывается в соответствующую статистику (blocked/
errors), листинг просто останется в выборке следующего прогона (WHERE
floor IS NULL И NOT EXISTS ... — тот же listing снова отберётся, отдельного
"уже пробовали" маркера не заводим, задача этого не просит).

## Процедура запуска (задача, явно)

    venv/bin/python scripts/backfill_listing_floors.py --dry-run --limit 20      # canary, не пишет
    venv/bin/python scripts/backfill_listing_floors.py --limit 20                # canary, реальная запись
    # canary чист (низкий errors/blocked, floor_filled нетривиален) ->
    venv/bin/python scripts/backfill_listing_floors.py --limit 200 --batch-size 100
    # полный оставшийся бэклог -> без --limit (обрабатывает всё найденное на момент SELECT)
    venv/bin/python scripts/backfill_listing_floors.py

После заполнения — НЕ запускать полный property backfill (scripts/
backfill_property_ids.py) поверх: incremental job (bot/jobs/
property_identity_incremental.py, systemd-таймер krisha-property-identity-
incremental) сам подбирает "unlinked" listing'и (NOT EXISTS property_
listings) на следующем прогоне — тем же WHERE NOT EXISTS, которым и этот
скрипт нашёл их. Задача явно просит ПРОВЕРИТЬ это, не запускать вручную —
см. --verify-incremental ниже и отчёт задачи.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import random
import sys
import time

# scripts/ — sys.path[0] по умолчанию = сам этот каталог, "from bot...."
# не резолвится без явного добавления корня репозитория (тот же приём,
# что scripts/backfill_property_ids.py).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.FileHandler("backfill_listing_floors.log", encoding="utf-8", errors="replace"),
              logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("backfill_listing_floors")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

_DELAY_RANGE = (8.0, 15.0)          # между объявлениями, тот же диапазон, что coord_backfill.py
_MAX_RETRIES = 3
_RETRY_DELAYS = (5.0, 15.0, 45.0)   # generic-ошибка (timeout/DNS/парсинг)
_BLOCK_MAX_RETRIES = 1
_BLOCK_RETRY_DELAYS = (90.0,)       # ListingBlockedError — редко и не спеша


class Stats:
    def __init__(self) -> None:
        self.found = 0
        self.floor_filled = 0
        self.floor_not_found = 0
        self.unavailable = 0
        self.blocked = 0
        self.errors = 0

    def as_dict(self) -> dict:
        return {
            "found": self.found, "floor_filled": self.floor_filled,
            "floor_not_found": self.floor_not_found, "unavailable": self.unavailable,
            "blocked": self.blocked, "errors": self.errors,
        }


async def _select_targets(limit: int | None, listing_id: str | None) -> list[dict]:
    """Прямая выборка — та же формула, что задача явно требует для аудита
    orphan properties (NOT EXISTS, не вычитание count'ов) — здесь для
    ЦЕЛЕВОГО набора backfill'а, тот же принцип "не гадать по разности".

    ORDER BY al.id::bigint — НЕ al.id (TEXT): найдено на первом же canary-
    прогоне (20 объявлений, --dry-run) — лексикографическая сортировка id
    как строки даёт вырожденный, НЕПРЕДСТАВИТЕЛЬНЫЙ срез (id разной длины,
    "1000176884" < "762070505" лексикографически, хотя численно наоборот
    — тот же класс проблемы, что bot/identity/property_linker.py::
    _canonical_rank уже документирует и чинит для порядка backfill'а
    кандидатов). На практике LIMIT 20 по al.id (TEXT) взял кластер id
    "1000xxxxxx" — как оказалось, все 20 без исключения были developer-
    presale объявлениями БЕЗ поля "этаж" на странице вообще (не ошибка
    скрипта — страница реально его не содержит, проверено вручную). Все
    id в apartment_listings числовые (проверено: 0 строк не проходят
    id ~ '^[0-9]+$' во всей продовой таблице) — НО тестовые фикстуры
    (tests/test_backfill_listing_floors.py) намеренно используют
    нечисловые '__test_...__' id (тот же паттерн, что весь остальной
    проект, см. tests/test_property_identity_incremental.py), поэтому
    голый al.id::bigint падал бы InvalidTextRepresentationError на них
    (найдено при первом прогоне тестов) — CASE WHEN приводит числовые id
    к bigint для сортировки, нечисловые сортируются строкой ПОСЛЕ них
    (NULLS LAST), не роняя запрос."""
    from bot.db.pg import fetch

    if listing_id is not None:
        rows = await fetch(
            "SELECT id, url FROM apartment_listings WHERE id = $1 AND floor IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM property_listings pl WHERE pl.listing_id = apartment_listings.id)",
            listing_id,
        )
        return [dict(r) for r in rows]

    sql = (
        "SELECT id, url FROM apartment_listings al WHERE al.floor IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM property_listings pl WHERE pl.listing_id = al.id) "
        "ORDER BY CASE WHEN al.id ~ '^[0-9]+$' THEN al.id::bigint END NULLS LAST, al.id"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = await fetch(sql)
    return [dict(r) for r in rows]


async def _fetch_with_retry(url: str, stats: Stats) -> dict | None:
    """None — исчерпаны retries (blocked ИЛИ error, уже засчитано в stats).
    dict — успешный fetch (может быть archived, caller решает по is_archived)."""
    from bot.core.apartment_details import fetch_apartment_details, ListingBlockedError

    for attempt in range(_BLOCK_MAX_RETRIES + 1):
        try:
            return await fetch_apartment_details(url, raise_on_error=True)
        except ListingBlockedError as e:
            if attempt >= _BLOCK_MAX_RETRIES:
                log.warning("blocked (retries исчерпаны) %s: %s", url, e)
                stats.blocked += 1
                return None
            delay = _BLOCK_RETRY_DELAYS[min(attempt, len(_BLOCK_RETRY_DELAYS) - 1)]
            log.warning("blocked %s, retry через %.0fс (%d/%d)", url, delay, attempt + 1, _BLOCK_MAX_RETRIES)
            await asyncio.sleep(delay)
        except Exception:
            break  # generic-ошибка — отдельная retry-лестница ниже
    else:
        return None

    for attempt in range(_MAX_RETRIES):
        try:
            return await fetch_apartment_details(url, raise_on_error=True)
        except Exception as e:  # noqa: BLE001 — намеренно широкий catch: любая сетевая/парсинг ошибка
            if attempt >= _MAX_RETRIES - 1:
                log.warning("error (retries исчерпаны) %s: %s: %s", url, type(e).__name__, e)
                stats.errors += 1
                return None
            delay = _RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)]
            log.warning("error %s: %s — retry через %.0fс (%d/%d)",
                        url, type(e).__name__, delay, attempt + 1, _MAX_RETRIES)
            await asyncio.sleep(delay)
    return None


def classify_and_extract(details: dict) -> tuple[str, int | None, int | None]:
    """(outcome, floor, floors_total) — чистая функция от результата
    fetch_apartment_details(), не трогает БД/сеть, отдельно тестируется.
    outcome: 'floor_filled' | 'floor_not_found' | 'unavailable'."""
    if details.get("is_archived"):
        return "unavailable", None, None
    floor = details.get("floor")
    if floor is None:
        return "floor_not_found", None, None
    return "floor_filled", floor, details.get("floors_total")


async def run_backfill(limit: int | None, batch_size: int, dry_run: bool,
                        listing_id: str | None) -> Stats:
    from bot.db.pg import execute

    stats = Stats()
    targets = await _select_targets(limit, listing_id)
    stats.found = len(targets)
    log.info("найдено %d listing'ов (floor IS NULL, без property_listings)%s",
             stats.found, " [DRY-RUN]" if dry_run else "")

    for i, row in enumerate(targets):
        lid, url = row["id"], row["url"]
        if not url:
            log.warning("%s: нет url, пропускаю", lid)
            stats.errors += 1
            continue

        details = await _fetch_with_retry(url, stats)
        if details is None:
            continue  # уже засчитано в stats (blocked/errors) внутри _fetch_with_retry

        outcome, floor, floors_total = classify_and_extract(details)
        if outcome == "unavailable":
            stats.unavailable += 1
        elif outcome == "floor_not_found":
            stats.floor_not_found += 1
        else:
            stats.floor_filled += 1
            if not dry_run:
                # WHERE floor IS NULL — доп. защита от гонки с coord_backfill.py/
                # живым парсером, которые тоже могут писать floor параллельно;
                # НИЧЕГО кроме floor/floors_total не трогаем (задача, явно).
                await execute(
                    "UPDATE apartment_listings SET floor = $2, floors_total = $3 "
                    "WHERE id = $1 AND floor IS NULL",
                    lid, floor, floors_total,
                )

        if (i + 1) % max(batch_size, 1) == 0 or (i + 1) == len(targets):
            log.info("прогресс: %d/%d — %s", i + 1, len(targets), stats.as_dict())

        if i < len(targets) - 1:
            await asyncio.sleep(random.uniform(*_DELAY_RANGE))

    return stats


async def verify_incremental_picks_up(sample_size: int = 5) -> dict:
    """Задача, явно: "после заполнения не запускать полный property
    backfill: проверить, что incremental Property Identity подбирает эти
    объявления штатно". DRY-RUN вызов bot.jobs.property_identity_
    incremental.run_incremental(dry_run=True) на небольшой выборке только
    что заполненных listing'ов (floor теперь NOT NULL, но всё ещё нет
    property_listings) — если несколько из них дают property_match_
    candidates/bootstrap в dry-run отчёте, incremental job их видит без
    отдельного полного backfill'а. НИЧЕГО не пишет (dry_run=True)."""
    from bot.db.pg import fetch
    from bot.jobs.property_identity_incremental import run_incremental

    sample = await fetch(
        "SELECT id FROM apartment_listings al WHERE al.floor IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM property_listings pl WHERE pl.listing_id = al.id) "
        "ORDER BY al.id DESC LIMIT $1",
        sample_size,
    )
    ids = [r["id"] for r in sample]
    if not ids:
        return {"sample": [], "note": "нет свежезаполненных unlinked listing'ов для проверки"}
    report = await run_incremental(dry_run=True, listing_ids=ids)
    return {"sample": ids, "incremental_dry_run_report": report}


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="ничего не писать, только статистика")
    ap.add_argument("--limit", type=int, default=None, help="ограничить выборку этого прогона")
    ap.add_argument("--batch-size", type=int, default=100, help="частота прогресс-лога (не отдельная транзакция)")
    ap.add_argument("--listing-id", type=str, default=None, help="обработать один конкретный listing_id")
    ap.add_argument("--verify-incremental", action="store_true",
                     help="после backfill'а — dry-run incremental job на свежих listing'ах, отчёт, без записи")
    args = ap.parse_args()

    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    t0 = time.monotonic()
    try:
        stats = await run_backfill(args.limit, args.batch_size, args.dry_run, args.listing_id)
        verify_report = None
        if args.verify_incremental and not args.dry_run:
            verify_report = await verify_incremental_picks_up()
    finally:
        await close_pool()

    elapsed = round(time.monotonic() - t0, 1)
    log.info("ИТОГ (%s, %.1fс): %s", "DRY-RUN" if args.dry_run else "запись", elapsed, stats.as_dict())
    print({"dry_run": args.dry_run, "elapsed_sec": elapsed, **stats.as_dict()})
    if verify_report is not None:
        print({"verify_incremental": verify_report})


if __name__ == "__main__":
    asyncio.run(main())
