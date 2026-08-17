#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bot/jobs/property_identity_incremental.py — инкрементальное поддержание
Property Identity для НОВЫХ apartment_listings (задача 2026-08-17,
"поддерживать Property Identity инкрементально после production backfill").

## Проверено ПЕРЕД тем, как писать этот файл (задача явно требует
доказательства, не предположения)

НИ krisha-apartments.service (service_apartments.py), НИ какой-либо
другой live-сервис НЕ вызывает bot.identity.property_linker.link_
listing_to_property после вставки новой apartment_listings-строки:

  1. Граф вызовов (codebase-memory-mcp query_graph): единственные caller'ы
     link_listing_to_property — scripts/backfill_property_ids.py и
     тестовые файлы. service_apartments.py на link_listing_to_property/
     property_linker/backfill_property_ids/property_id не ссылается
     вообще (текстовый grep — 0 совпадений).
  2. Живое наблюдение (после production backfill, PR #8): парсер
     (krisha-apartments.service) вставил 282 новых apartment_listings
     между рестартом сервиса и проверкой — 0 из них получили строку в
     property_listings, properties/property_listings/property_match_
     candidates не изменились НИ НА ОДНУ строку. Задокументировано в
     финальном отчёте задачи "production backfill".

Значит — ОТДЕЛЬНЫЙ инкрементальный job (задача, явно: "не утяжелять
парсер" — парсер остаётся про парсинг, идентити-связывание — своя,
независимо перезапускаемая ответственность).

## Что переиспользуется, что нет

Использует ТЕ ЖЕ функции, что и scripts/backfill_property_ids.py для
match_mode="candidate_only" — bot.identity.property_linker::
bootstrap_all_provisional (Фаза A) + generate_all_candidates (Фаза B).
Тот же matcher_version "candidate_only_v1", НИКАКОГО нового matcher'а
(задача, явно). НИКАКИХ hard merge: Фаза A только создаёт provisional
property (link_method='bootstrap'), Фаза B только пишет property_match_
candidates со status pending/rejected — pending/rejected НЕ принимаются
автоматически ни этим job'ом, ни где-либо ещё в проекте на сейчас
(ручной review — отдельная будущая задача, вне этого PR).

## Инкрементальность — БЕЗ отдельной "новые/старые" логики

Три категории пар из задачи получаются даром из УЖЕ СУЩЕСТВУЮЩЕЙ
конструкции bootstrap_all_provisional/generate_all_candidates (то же
разделение, что уже доказано детерминированным для полного backfill —
см. PR "fix(identity): deterministic two-phase candidate graph"),
просто за счёт того, ЧТО именно попадает в rows этого запуска
(_fetch_unlinked — см. ниже):
  - старые-старые: listing'и, у которых УЖЕ есть property_listings,
    вообще не входят в rows -> generate_all_candidates их даже не
    видит как anchor — пары физически не пересчитываются.
  - новые-новые: оба входят в ОДИН mapping этого запуска -> находят
    друг друга через by_hash/by_complex_floor/dedup-партнёров ВНУТРИ
    generate_all_candidates, ПОСЛЕ того как Фаза A этого запуска
    полностью завершилась (та же гарантия "видят друг друга только
    после конца Фазы A", что уже протестирована для backfill'а).
  - новые-старые: generate_all_candidates ищет их через РЕАЛЬНУЮ БД
    (_find_exact_hash_properties/_find_fuzzy_properties/_find_all_
    dedup_partners — независимо от текущего mapping), та же ветка кода,
    что уже обеспечивает идемпотентность повторного/добавочного
    backfill'а (см. её же докстринг: "плюс отдельно — среди РЕАЛЬНО уже
    существующих properties из ПРЕДЫДУЩИХ прогонов").

skipped (адрес/этаж/площадь неизвестны) НЕ помечается нигде отдельно —
такой listing просто не получает property_listings строку, значит
СНОВА попадёт в "unlinked" на следующем запуске автоматически (задача,
явно: "поля могли заполниться позже") — без отдельного механизма
перепроверки, это прямое следствие того, что "unlinked" определяется
через NOT EXISTS property_listings, а не через какой-то отдельный флаг.

## Advisory lock

pg_try_advisory_lock — НЕ блокирующий (задача, явно: "одновременно
работает только один экземпляр"; это периодический таймер, не очередь,
конкурентный запуск должен ВЫЙТИ, а не ждать). Отдельный ключ
(_ADVISORY_LOCK_KEY), НЕ совпадает с ключом миграций (bot/db/pg.py,
728140, тот блокирующий и про другое).

Запуск:
    venv/bin/python -m bot.jobs.property_identity_incremental --dry-run
    venv/bin/python -m bot.jobs.property_identity_incremental
    venv/bin/python -m bot.jobs.property_identity_incremental --limit 500 --dry-run
    venv/bin/python -m bot.jobs.property_identity_incremental --since 2026-08-17T00:00:00 --verbose
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.FileHandler("property_identity_incremental.log", encoding="utf-8", errors="replace")],
)
log = logging.getLogger("property_identity_incremental")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

# Отдельный advisory-lock ключ — НЕ 728140 (bot/db/pg.py, миграции, тот
# блокирующий и про другое, коллизия исключена явно: единственный другой
# ключ в проекте на сейчас).
_ADVISORY_LOCK_KEY = 728151

# match_method -> ключ в отчёте (тот же приём, что _CANDIDATE_STAT_KEY в
# scripts/backfill_property_ids.py — короче для отчёта, НЕ равно
# match_method буквально).
_CANDIDATE_STAT_KEY = {"exact_hash": "exact_candidates", "fuzzy": "fuzzy_candidates",
                       "dedup_listings": "dedup_candidates"}

_COLUMNS = ("id, address, floor, area, rooms, complex_name, first_seen, last_seen, archived_at, "
            "price, seller_name, is_duplicate, duplicate_of, dup_match")

# Мониторинг (задача, п.4) — пороги для warning-логов ниже.
_COVERAGE_WARN_THRESHOLD = 0.95
_BACKLOG_WARN_THRESHOLD = 1000


async def _acquire_lock():
    """pg_try_advisory_lock на ВЫДЕЛЕННОМ соединении — НЕ через execute()/
    fetchval() (bot/db/pg.py): те берут соединение из пула НА ОДИН запрос
    и сразу возвращают его обратно в пул, лок с ним потерялся бы для
    caller'а немедленно (следующий же execute() мог бы получить ДРУГОЕ
    соединение из пула, без лока). Возвращает соединение (держать до
    конца прогона, передать в _release_lock) либо None, если лок уже
    занят другим экземпляром — caller обязан в этом случае выйти без
    работы, НЕ ждать (задача: "одновременно работает только один
    экземпляр", периодический таймер — не очередь)."""
    from bot.db.pg import get_pool
    conn = await get_pool().acquire()
    got = await conn.fetchval("SELECT pg_try_advisory_lock($1)", _ADVISORY_LOCK_KEY)
    if not got:
        await get_pool().release(conn)
        return None
    return conn


async def _release_lock(conn) -> None:
    from bot.db.pg import get_pool
    try:
        await conn.fetchval("SELECT pg_advisory_unlock($1)", _ADVISORY_LOCK_KEY)
    finally:
        await get_pool().release(conn)


async def _fetch_unlinked(limit: int | None, since: datetime | None,
                           listing_ids: list[str] | None = None) -> list[dict]:
    """Снимок (ОДИН SELECT, в память) listing'ов БЕЗ property_listings —
    зафиксирован В НАЧАЛЕ прогона (задача, явно: "в начале фиксирует
    список listings без property_listings"). Дальше Фаза A/B работают
    ТОЛЬКО с этим списком — новые вставки парсера ПОСЛЕ этого момента
    просто попадут в СЛЕДУЮЩИЙ запуск джоба (тот же принцип, что уже был
    у scripts/backfill_property_ids.py: rows читаются один раз, не
    перезапрашиваются по ходу прогона — стабильный source-снимок).

    since — опциональная нижняя граница first_seen (флаг --since,
    задача п.2) — сужает выборку для отладки/тестов; в штатной работе
    (без --since) обрабатывается ВЕСЬ backlog unlinked, не только
    "недавние".

    listing_ids — опциональный скоуп ТОЛЬКО для дешёвых тестов (тот же
    приём, что scripts/backfill_property_ids.py::run_backfill —
    "прод-путь им не пользуется"): без него прогон в тесте задел бы
    ВЕСЬ реальный backlog unlinked на dev-БД, не только тестовые
    listing'и."""
    from bot.db.pg import fetch
    sql = (f"SELECT {_COLUMNS} FROM apartment_listings al "
           "WHERE NOT EXISTS (SELECT 1 FROM property_listings pl WHERE pl.listing_id = al.id)")
    params: list = []
    if listing_ids is not None:
        params.append(listing_ids)
        sql += f" AND al.id = ANY(${len(params)}::text[])"
    if since is not None:
        params.append(since)
        sql += f" AND al.first_seen >= ${len(params)}"
    sql += " ORDER BY al.id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = await fetch(sql, *params)
    return [dict(r) for r in rows]


async def _coverage() -> float:
    """listings с property_listings / все apartment_listings — грубая,
    но дешёвая метрика "насколько Property Identity успевает за
    потоком" (задача, п.2 отчёта и п.4 мониторинга)."""
    from bot.db.pg import fetchval
    total = await fetchval("SELECT count(*) FROM apartment_listings")
    linked = await fetchval("SELECT count(*) FROM property_listings")
    return round(linked / total, 4) if total else 1.0


def _canonicalize_candidates(candidates: list[dict]) -> dict[tuple, set]:
    """pair(listing_id, other_listing_id) -> {(method, type, status), ...}
    — если для ОДНОЙ пары в ЭТОМ прогоне набралось 2+ РАЗНЫХ варианта,
    это internal conflict (задача, п.4 мониторинга: "internal candidate
    conflicts >0"). Структурно должно быть 0 (generate_all_candidates
    гарантирует ровно одно направление на пару через _canonical_rank —
    см. её докстринг), эта проверка — defensive, не единственная
    гарантия. Работает ОДИНАКОВО в dry-run и в реальной записи (не
    зависит от того, что уже в БД — только от того, что ЭТОТ прогон
    сгенерировал в памяти)."""
    pairs: dict[tuple, set] = {}
    for c in candidates:
        other = c.get("other_listing_id")
        if not other:
            continue
        key = tuple(sorted([c["listing_id"], other]))
        pairs.setdefault(key, set()).add((c["match_method"], c["relationship_type"], c["status"]))
    return pairs


async def _hard_link_count() -> int:
    """link_method != 'bootstrap' где-либо в property_listings — для
    match_mode=candidate_only структурно невозможно (bootstrap_all_
    provisional хардкодит 'bootstrap' в INSERT, задача п.4: "появился
    hard-link method"). Таблично-широкая проверка (не только строки
    ЭТОГО прогона) — дёшево (один count), сильнее как monitoring-сигнал."""
    from bot.db.pg import fetchval
    return await fetchval("SELECT count(*) FROM property_listings WHERE link_method <> 'bootstrap'")


async def run_incremental(dry_run: bool = False, limit: int | None = None,
                           since: datetime | None = None, verbose: bool = False,
                           listing_ids: list[str] | None = None) -> dict:
    """Один прогон инкрементального job'а. Возвращает отчёт-словарь (те
    же поля, что просит CLI-отчёт задачи, п.2). dry_run=True — НИ ОДНОЙ
    записи в БД (bootstrap_all_provisional это уже гарантирует для Фазы
    A; Фаза B здесь пишет candidates ТОЛЬКО в ветке "if not dry_run" —
    generate_all_candidates сама никогда не пишет, только возвращает
    записи в память, см. её докстринг в property_linker.py).

    listing_ids — см. _fetch_unlinked: скоуп ТОЛЬКО для тестов, прод-CLI
    (main() ниже) им не пользуется."""
    from bot.db.pg import execute
    from bot.identity.property_linker import bootstrap_all_provisional, generate_all_candidates

    t0 = time.monotonic()
    coverage_before = await _coverage()
    hard_links_before = await _hard_link_count()

    lock_conn = await _acquire_lock()
    if lock_conn is None:
        log.warning("advisory lock (key=%s) занят другим экземпляром — выхожу без работы", _ADVISORY_LOCK_KEY)
        return {"status": "skipped_locked", "dry_run": dry_run, "unlinked_found": 0,
                "coverage_before": coverage_before, "coverage_after": coverage_before}

    try:
        rows = await _fetch_unlinked(limit, since, listing_ids)
        unlinked_found = len(rows)
        if verbose:
            log.info("unlinked snapshot: %d listing'ов (limit=%s, since=%s)", unlinked_found, limit, since)

        if unlinked_found > _BACKLOG_WARN_THRESHOLD:
            log.warning("backlog unlinked=%d > %d — джоб не успевает за потоком новых listing'ов "
                        "(интервал таймера/производительность)", unlinked_found, _BACKLOG_WARN_THRESHOLD)

        mapping = await bootstrap_all_provisional(rows, dry_run)
        if verbose:
            log.info("Фаза A завершена: %d listing'ов обработано", len(mapping))

        provisional_created = 0
        skipped_by_reason: dict[str, int] = {}
        for m in mapping.values():
            if m["status"] == "bootstrap":
                provisional_created += 1
            elif m["status"] == "skipped":
                reason = m.get("skip_reason", "unknown")
                skipped_by_reason[reason] = skipped_by_reason.get(reason, 0) + 1

        candidates = await generate_all_candidates(mapping)
        if verbose:
            log.info("Фаза B завершена: %d candidate-записей сгенерировано", len(candidates))

        cand_stats = {"exact_candidates": 0, "fuzzy_candidates": 0, "dedup_candidates": 0, "rejected_by_conflict": 0}
        for c in candidates:
            cand_stats[_CANDIDATE_STAT_KEY[c["match_method"]]] += 1
            if c.get("rejected_by_conflict"):
                cand_stats["rejected_by_conflict"] += 1
            if not dry_run:
                await execute(
                    """
                    INSERT INTO property_match_candidates
                        (listing_id, candidate_property_id, match_method, match_score, relationship_type,
                         evidence, conflict_reasons, matcher_version, status)
                    VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8, $9)
                    ON CONFLICT (listing_id, candidate_property_id) DO NOTHING
                    """,
                    c["listing_id"], c["candidate_property_id"], c["match_method"], c["match_score"],
                    c["relationship_type"], json.dumps(c["evidence"], default=str, ensure_ascii=False),
                    json.dumps(c["conflict_reasons"], ensure_ascii=False) if c["conflict_reasons"] else None,
                    c["matcher_version"], c["status"],
                )

        internal_conflict_pairs = _canonicalize_candidates(candidates)
        internal_conflicts = sum(1 for variants in internal_conflict_pairs.values() if len(variants) > 1)
        if internal_conflicts > 0:
            log.warning("internal candidate conflicts=%d > 0 — не ожидалось, см. bot/identity/"
                        "property_linker.py::generate_all_candidates", internal_conflicts)

        hard_links_after = await _hard_link_count() if not dry_run else hard_links_before
        if hard_links_after > hard_links_before:
            log.warning("hard-link (link_method != 'bootstrap') появился: %d -> %d — не ожидалось для "
                        "match_mode=candidate_only", hard_links_before, hard_links_after)

        coverage_after = await _coverage() if not dry_run else coverage_before
        if coverage_after < _COVERAGE_WARN_THRESHOLD:
            log.warning("coverage=%.4f < %.2f — растущая доля listing'ов без property_id",
                        coverage_after, _COVERAGE_WARN_THRESHOLD)

        elapsed = round(time.monotonic() - t0, 2)
        report = {
            "status": "ok",
            "dry_run": dry_run,
            "unlinked_found": unlinked_found,
            "provisional_created": provisional_created,
            "skipped_by_reason": skipped_by_reason,
            "skipped_total": sum(skipped_by_reason.values()),
            "exact_candidates": cand_stats["exact_candidates"],
            "fuzzy_candidates": cand_stats["fuzzy_candidates"],
            "dedup_candidates": cand_stats["dedup_candidates"],
            "rejected_by_conflict": cand_stats["rejected_by_conflict"],
            "internal_conflicts": internal_conflicts,
            "hard_links_total": hard_links_after,
            "elapsed_sec": elapsed,
            "coverage_before": coverage_before,
            "coverage_after": coverage_after,
        }
    finally:
        await _release_lock(lock_conn)

    if not dry_run:
        # app_settings (bot/db/settings.py) — тот же паттерн, что уже
        # используют другие сервисы для операционного статуса
        # (APARTMENT_SERVICE_GIT_HASH и т.п., см. service_apartments.py)
        # — естественное место для внешнего мониторинга/дашборда читать
        # "когда последний раз бегал, что нашёл", без отдельной таблицы.
        from bot.db import settings as app_settings
        await app_settings.set("PROPERTY_IDENTITY_INCREMENTAL_LAST_RUN", datetime.now(timezone.utc).isoformat())
        await app_settings.set("PROPERTY_IDENTITY_INCREMENTAL_LAST_STATUS", report["status"])
        await app_settings.set("PROPERTY_IDENTITY_INCREMENTAL_LAST_COVERAGE", str(report["coverage_after"]))
        await app_settings.set("PROPERTY_IDENTITY_INCREMENTAL_LAST_UNLINKED", str(report["unlinked_found"]))

    return report


def _parse_since(value: str | None) -> datetime | None:
    """'YYYY-MM-DD' или полный ISO datetime; наивные даты трактуются как
    UTC (first_seen — timestamptz, тот же приём, что bot/jobs/deal_score_
    backtest.py::_parse_as_of)."""
    if value is None:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="ничего не писать, только сводка")
    ap.add_argument("--limit", type=int, default=None, help="ограничить выборку unlinked (отладка)")
    ap.add_argument("--since", type=str, default=None,
                     help="учитывать только listing'и с first_seen >= ISO-дата/datetime "
                          "(по умолчанию — весь backlog unlinked, не только недавние)")
    ap.add_argument("--verbose", action="store_true", help="подробный лог (снимок/прогресс по фазам)")
    args = ap.parse_args()
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
        log.setLevel(logging.DEBUG)

    since = _parse_since(args.since)

    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    try:
        report = await run_incremental(dry_run=args.dry_run, limit=args.limit, since=since, verbose=args.verbose)
    except Exception:
        # "job падает" (задача, п.4 мониторинга) — лог с трейсбеком +
        # ненулевой exit code (systemd показывает unit как failed,
        # внешний мониторинг это видит через systemctl is-failed), плюс
        # статус в app_settings — тем же путём, что штатный отчёт, чтобы
        # дашборд не читал "последний OK" молча вместо факта падения.
        log.error("job упал с исключением", exc_info=True)
        try:
            from bot.db import settings as app_settings
            await app_settings.set("PROPERTY_IDENTITY_INCREMENTAL_LAST_STATUS", "error")
            await app_settings.set("PROPERTY_IDENTITY_INCREMENTAL_LAST_RUN", datetime.now(timezone.utc).isoformat())
        except Exception:
            pass
        await close_pool()
        sys.exit(1)
    else:
        await close_pool()

    log.info("ИТОГ (%s): %s", "DRY-RUN, ничего не записано" if args.dry_run else "запись выполнена", report)
    print(report)


if __name__ == "__main__":
    asyncio.run(main())
