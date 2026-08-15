#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Двухуровневый matching реестра КЖК -> наши сущности (задача
2026-08-15, коммит 3).

**Уровень 1** — `kzk_registry` -> `developers`: сначала по БИН (точный,
надёжный), затем fuzzy по бренду/юрлицу против `developers.name`+
`aliases`, ТЕ ЖЕ пороги, что уже установлены для ЖК-уровня Entity
Resolution (`bot/core/entity_resolution.py::AUTO_MATCH_THRESHOLD=0.8`/
`REVIEW_QUEUE_THRESHOLD=0.5`, Фаза B п.4 ER-калибровка) — не изобретаем
вторые пороги для той же задачи.

**Уровень 2** — редкие `zhk_names` (142/313 записей на разведке) ->
`complexes`, тот же fuzzy-приём, по каждому названию отдельно, но БЕЗ
подтверждённого действия — только кандидат в `zhk_matches` (JSONB),
подтверждение/отклонение — ручное действие на будущей админ-странице
(коммит 4).

**Матчинг SQL-side, не Python-циклом по name_similarity().** `bot/
core/entity_resolution.py::name_similarity()` делает ОДИН SQL-запрос
НА ПАРУ имён — для 313 kzk-записей × 514 developers это было бы
~161 тыс. запросов, непрактично медленно. Здесь — один SQL-запрос НА
kzk-запись (`ORDER BY similarity(...) DESC LIMIT 1`), вся работа
(сравнение со ВСЕМИ кандидатами) — внутри Postgres через pg_trgm
`similarity()` (то же расширение, что уже использует `name_
similarity()` — переиспользуем механизм, не Python-обёртку).

**developer_id пишется и в review-режиме, не только auto** — иначе
админ-странице (коммит 4) нечего было бы предложить на подтверждение
(«вот кандидат Х, подтвердить?»); `developer_match_method` различает
`'bin'` / `'name_fuzzy_auto'` (применено) / `'name_fuzzy_review'`
(кандидат, не подтверждён) / `NULL` (ничего не найдено).

Расписание: НЕТ отдельного таймера — вызывается вручную/по требованию
(matching после ручного добавления developers.bin или после нового
прогона collect'а — задача не просила автоматизировать этот шаг).
Разовая проверка: venv/bin/python kzk_registry_match.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.FileHandler("kzk_registry_match.log", encoding="utf-8", errors="replace")],
)
log = logging.getLogger("kzk_registry_match")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

# Лучший кандидат-застройщик по максимуму similarity() среди name+aliases
# ПРОТИВ обоих (brand, legal) сразу — GREATEST внутри, MAX между
# кандидатами одного developer_id (alias мог совпасть лучше, чем name).
_DEV_MATCH_SQL = """
    WITH dev_candidates AS (
        SELECT d.id, unnest(array_append(COALESCE(d.aliases, ARRAY[]::text[]), d.name)) AS cand_name
        FROM developers d
    )
    SELECT id, MAX(GREATEST(
        similarity(lower(trim($1)), lower(trim(cand_name))),
        similarity(lower(trim($2)), lower(trim(cand_name)))
    )) AS sim
    FROM dev_candidates
    GROUP BY id
    ORDER BY sim DESC
    LIMIT 1
"""

_COMPLEX_MATCH_SQL = """
    SELECT id, similarity(lower(trim($1)), lower(trim(name))) AS sim
    FROM complexes
    WHERE COALESCE(is_garbage, FALSE) = FALSE
    ORDER BY sim DESC
    LIMIT 1
"""


async def match_developers(kzk_ids: list[int] | None = None) -> dict:
    """Уровень 1. kzk_ids — опциональный скоуп ТОЛЬКО для тестов (тот
    же паттерн, что у остальных скриптов этой сессии) — прод-путь
    (None) идёт по всем записям без developer_id."""
    from bot.db.pg import fetch, fetchrow, execute
    from bot.core.entity_resolution import AUTO_MATCH_THRESHOLD, REVIEW_QUEUE_THRESHOLD

    if kzk_ids is not None:
        rows = await fetch(
            "SELECT id, bin, developer_brand, developer_legal FROM kzk_registry WHERE id = ANY($1::int[])",
            kzk_ids)
    else:
        rows = await fetch(
            "SELECT id, bin, developer_brand, developer_legal FROM kzk_registry WHERE developer_id IS NULL")

    by_bin = auto_fuzzy = review = unresolved = 0
    for r in rows:
        # БИН — точный, надёжный матч, приоритет 1 (аналог 'bin' в
        # тестах Фазы L1: единственный способ 100% доверия).
        dev = await fetchrow("SELECT id FROM developers WHERE bin = $1", r["bin"]) if r["bin"] else None
        if dev:
            await execute(
                "UPDATE kzk_registry SET developer_id=$2, developer_match_method='bin' WHERE id=$1",
                r["id"], dev["id"])
            by_bin += 1
            continue

        brand = r["developer_brand"] or ""
        legal = r["developer_legal"] or ""
        if not brand and not legal:
            unresolved += 1
            continue
        best = await fetchrow(_DEV_MATCH_SQL, brand, legal)
        sim = float(best["sim"]) if best and best["sim"] is not None else 0.0

        if sim >= AUTO_MATCH_THRESHOLD:
            await execute(
                "UPDATE kzk_registry SET developer_id=$2, developer_match_method='name_fuzzy_auto' WHERE id=$1",
                r["id"], best["id"])
            auto_fuzzy += 1
        elif sim >= REVIEW_QUEUE_THRESHOLD:
            # Кандидат сохраняется (не только пометка) — иначе админ-
            # странице (коммит 4) нечего предложить на подтверждение.
            await execute(
                "UPDATE kzk_registry SET developer_id=$2, developer_match_method='name_fuzzy_review' WHERE id=$1",
                r["id"], best["id"])
            review += 1
        else:
            unresolved += 1

    log.info("match_developers: %d записей, bin=%d, fuzzy_auto=%d, review=%d, unresolved=%d",
              len(rows), by_bin, auto_fuzzy, review, unresolved)
    return {"total": len(rows), "bin": by_bin, "fuzzy_auto": auto_fuzzy, "review": review, "unresolved": unresolved}


async def match_zhk_names(kzk_ids: list[int] | None = None) -> dict:
    """Уровень 2. Только записи с непустым zhk_names (редкое поле, см.
    migrations/074) — per-название отдельный SQL-запрос, обычно 1
    название на запись."""
    from bot.db.pg import fetch, fetchrow, execute
    from bot.core.entity_resolution import AUTO_MATCH_THRESHOLD, REVIEW_QUEUE_THRESHOLD

    if kzk_ids is not None:
        rows = await fetch(
            "SELECT id, zhk_names FROM kzk_registry WHERE id = ANY($1::int[]) "
            "AND zhk_names IS NOT NULL AND zhk_names != '[]'::jsonb",
            kzk_ids)
    else:
        rows = await fetch(
            "SELECT id, zhk_names FROM kzk_registry WHERE zhk_names IS NOT NULL AND zhk_names != '[]'::jsonb")

    matched = pending = 0
    for r in rows:
        names = r["zhk_names"]
        names = json.loads(names) if isinstance(names, str) else names
        matches = []
        for name in names or []:
            best = await fetchrow(_COMPLEX_MATCH_SQL, name)
            sim = float(best["sim"]) if best and best["sim"] is not None else 0.0
            if sim >= AUTO_MATCH_THRESHOLD:
                matches.append({"name": name, "complex_id": best["id"], "method": "auto", "confidence": round(sim, 3)})
                matched += 1
            elif sim >= REVIEW_QUEUE_THRESHOLD:
                matches.append({"name": name, "complex_id": best["id"], "method": "review", "confidence": round(sim, 3)})
                pending += 1
            else:
                matches.append({"name": name, "complex_id": None, "method": None,
                                 "confidence": round(sim, 3) if best else 0.0})
                pending += 1
        await execute("UPDATE kzk_registry SET zhk_matches=$2::jsonb WHERE id=$1",
                      r["id"], json.dumps(matches, ensure_ascii=False))

    log.info("match_zhk_names: %d записей с zhk_names, %d названий сматчено (auto), %d review/не сматчено",
              len(rows), matched, pending)
    return {"total_records": len(rows), "matched": matched, "pending": pending}


async def match_kzk_to_complexes(kzk_ids: list[int] | None = None) -> dict:
    """Оркестратор — уровень 1 + уровень 2 разом."""
    dev_result = await match_developers(kzk_ids)
    zhk_result = await match_zhk_names(kzk_ids)
    return {"developers": dev_result, "zhk": zhk_result}


async def main() -> None:
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    try:
        result = await match_kzk_to_complexes()
        log.info("match_kzk_to_complexes итог: %s", result)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
