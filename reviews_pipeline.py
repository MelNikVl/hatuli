#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""reviews_pipeline.py — мульти-источниковый сбор отзывов на ЖК Астаны
(задача 2026-08-16, миграция 081: reviews_raw).

Оркестратор: принимает complex_id/developer_id, опрашивает ВСЕ источники
параллельно (asyncio.gather), дедуплицирует кросс-посты по
(author, review_date, text_hash), пишет СЫРЫЕ отзывы в reviews_raw.
LLM-классификация (sentiment/topics) — НЕ здесь: отдельная задача
(DeepSeek) по строкам classified_at IS NULL, см. миграцию 081.
Классификатору: строки-sentinel «отзывов нет» (review_text='',
raw->>'empty'='true') сразу пишутся с classified_at=now() — в очередь
классификации они не попадают, фильтровать не нужно.

Источники (порядок в _SOURCES = приоритет при дедупликации: кросс-пост
оставляем у первого источника, остальные — в raw->>'also_on'):
  - 2gis          — рабочий: переиспользует find_geo_id/fetch_reviews из
                    2gis_reviews_collect.py (SSR-скрапинг, вежливая пауза
                    между ЖК — см. _SLEEP_S);
  - google_maps   — ЗАГЛУШКА: нужен GOOGLE_MAPS_API_KEY в .env (Places
                    API: findplacefromtext -> place/details -> reviews).
                    Без ключа честно возвращает [] — точка расширения;
  - yandex        — ЗАГЛУШКА: публичного API отзывов нет, скрапинг
                    запрещён условиями Яндекса. Возвращает [].

Отличие от 2gis_reviews_collect.py: тот пишет КЛАССИФИЦИРОВАННЫЕ отзывы
2GIS в developer_reviews и никуда не расширяется; этот — сырой слой
всех источников в reviews_raw. Оба могут жить параллельно (разные
таблицы); когда reviews_pipeline покроет все ЖК, старый коллектор
планируется к выводу (не в этой задаче).

Инкрементальность: ЖК пересобирается, только если его строк в reviews_raw
нет вообще или они старше _REFRESH_DAYS (25 дней) — при ежедневном
таймере это даёт ~ежемесячное обновление на ЖК без долбёжки источников.

Запуск:
    venv/bin/python reviews_pipeline.py --limit 20          # первые 20 ЖК из очереди
    venv/bin/python reviews_pipeline.py --complex-id 117    # один ЖК
    venv/bin/python reviews_pipeline.py --developer-id 5    # все ЖК застройщика
    venv/bin/python reviews_pipeline.py --fast              # пауза 5с (тесты)

Расписание: krisha-reviews-collect.timer (ежедневно 03:00).
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("reviews_pipeline.log", encoding="utf-8", errors="replace"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("reviews_pipeline")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

# ЖК пересобирается не чаще, чем раз в столько дней (ежедневный таймер ×
# это окно = ~ежемесячное обновление на ЖК).
_REFRESH_DAYS = 25

# Вежливая пауза между ЖК — живой HTTP ходит только 2gis-скрапинг
# (2 запроса на ЖК: search + reviews). --fast для тестов.
_SLEEP_S = 30.0
_SLEEP_S_FAST = 5.0


def text_hash(text: str) -> str:
    """sha1 нормализованного текста — ключ дедупликации (миграция 081)."""
    norm = re.sub(r"\s+", " ", (text or "").strip().lower())
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()


def dedupe_reviews(reviews: list[dict]) -> list[dict]:
    """Кросс-источниковая дедупликация по (author, review_date, text_hash):
    один и тот же отзыв, скопированный автором в 2GIS и Google, остаётся
    ОДНОЙ строкой — у первого источника по порядку _SOURCES, остальные
    перечислены в raw->>'also_on' (не теряем факт кросс-постинга —
    это сигнал вовлечённости/накрутки для будущей классификации)."""
    seen: dict[tuple, dict] = {}
    out = []
    for r in reviews:
        key = ((r.get("author") or "").strip().lower(),
               r.get("review_date"), r["text_hash"])
        if key in seen:
            also = seen[key].setdefault("raw", {}).setdefault("also_on", [])
            if r["source"] not in also:
                also.append(r["source"])
            continue
        seen[key] = r
        out.append(r)
    return out


# ── Источники ─────────────────────────────────────────────────────────────
# Контракт коллектора: async (complex_row) -> list[dict] с ключами
# source, source_entity_id, author, review_date, rating, text, source_url,
# raw (dict|None). Пустой список — валидный ОТВЕТ («нет отзывов»), НЕ
# ошибка: конвейер обязан деградировать мягко (тот же принцип, что
# score_layers/osm.py). Задача 2026-08-17 ("ложный sentinel") —
# ВАЖНО отличать это от НЕУДАВШЕГОСЯ запроса: коллектор ДОЛЖЕН поднять
# исключение (TransientFetchError/PermanentFetchError из 2gis_reviews_
# collect.py, или любое другое), НЕ возвращать [] молча, если сам
# запрос не выполнился — collect_one_complex ниже полагается ИМЕННО на
# это различие, чтобы не писать sentinel «отзывов нет» на временный сбой.

async def collect_2gis(cx: dict) -> list[dict]:
    """2GIS — рабочий коллектор. Синхронный SSR-скрапинг
    (urllib в 2gis_reviews_collect.py) выносится в thread, чтобы не
    блокировать event loop при параллельном опросе источников.
    TransientFetchError/PermanentFetchError ПРОПАГИРУЮТСЯ (не ловим
    здесь) — collect_one_complex различает их через asyncio.gather(...,
    return_exceptions=True)."""
    import importlib
    mod = importlib.import_module("2gis_reviews_collect")
    geo = await asyncio.to_thread(mod.find_geo_id, cx["name"])
    if not geo:
        return []
    gid, _title = geo
    revs = await asyncio.to_thread(mod.fetch_reviews, gid)
    return [{
        "source": "2gis", "source_entity_id": gid,
        "author": r["author"], "review_date": r.get("date"),
        "rating": None, "text": r["text"],
        "source_url": f"https://2gis.kz/astana/geo/{gid}/tab/reviews",
        "raw": None,
    } for r in revs]


async def collect_google_maps(cx: dict) -> list[dict]:
    """ЗАГЛУШКА (точка расширения, задача DeepSeek): Google Places API —
    findplacefromtext по названию ЖК -> place/details (fields=reviews).
    Нужен GOOGLE_MAPS_API_KEY в .env; без него честно возвращаем []
    (Unknown ≠ average — не выдумываем данные)."""
    if not os.getenv("GOOGLE_MAPS_API_KEY"):
        return []
    log.warning("google_maps: ключ есть, но коллектор ещё не реализован (задача DeepSeek)")
    return []


async def collect_yandex(cx: dict) -> list[dict]:
    """ЗАГЛУШКА: у Яндекса нет публичного API отзывов, а скрапинг запрещён
    его условиями. Возвращаем [] — источник появится только при легальном
    доступе (партнёрский API)."""
    return []


# Порядок = приоритет при дедупликации (см. dedupe_reviews).
_SOURCES = [collect_2gis, collect_google_maps, collect_yandex]


def _classify_exc_status(exc: Exception) -> str:
    """Тип исключения -> "transient"/"permanent"/"unknown" — только для
    структурированного лога (задача 2026-08-17: "логировать source/
    status/error"), поведение (sentinel/нет) от этой строки НЕ зависит —
    ОБА типа ошибок одинаково НЕ пишут sentinel (см. collect_one_complex
    ниже), это только для читаемости лога/будущего дашборда."""
    import importlib
    mod = importlib.import_module("2gis_reviews_collect")
    if isinstance(exc, mod.TransientFetchError):
        return "transient"
    if isinstance(exc, mod.PermanentFetchError):
        return "permanent"
    return "unknown"


async def collect_one_complex(cx: dict, developer_id: int | None) -> dict:
    """Все источники параллельно -> дедупликация -> INSERT в reviews_raw.
    Возвращает статистику по ЖК. Классификации здесь НЕТ (задача
    DeepSeek): sentiment/topics/classified_at остаются NULL.

    Задача 2026-08-17 ("ложный sentinel"): различаем 4 исхода НА
    УРОВНЕ КАЖДОГО ИСТОЧНИКА, не агрегата по ЖК —
      - источник вернул N>=1 отзывов -> INSERT реальных строк;
      - источник УСПЕШНО ответил, вернул [] (0 отзывов) -> sentinel
        «отзывов нет» (единственный случай, когда sentinel пишется —
        задача, явно: "sentinel писать только после успешного ответа
        источника");
      - источник поднял исключение (timeout/5xx/429/ошибка парсинга —
        TransientFetchError/PermanentFetchError из 2gis_reviews_
        collect.py, или любое другое) -> НЕ sentinel, старые данные (по
        этому source) не трогаются, лог source/status/error, ЖК
        остаётся в очереди _queue() на следующий прогон (ежедневный
        таймер = естественный backoff, отдельного расписания не
        заводим — задача: "назначить retry с backoff", retry с backoff
        ВНУТРИ одной попытки уже сделан в 2gis_reviews_collect.py::get()).
    Раньше вся эта логика была НА УРОВНЕ ЖК целиком (`if not reviews:`) —
    сбой ОДНОГО источника среди прочих (или ошибка, проглоченная внутри
    find_geo_id/fetch_reviews) неотличимо превращался в sentinel для
    ВСЕХ источников."""
    from bot.db.pg import execute

    per_source = await asyncio.gather(
        *(src(cx) for src in _SOURCES), return_exceptions=True)

    reviews: list[dict] = []
    by_source: dict[str, int] = {}
    empty_sources: list[str] = []
    failed_sources: list[str] = []
    for src, res in zip(_SOURCES, per_source):
        name = src.__name__.removeprefix("collect_")
        if isinstance(res, Exception):
            failed_sources.append(name)
            log.warning("reviews_pipeline: source=%s status=%s complex_id=%s error=%s",
                        name, _classify_exc_status(res), cx["id"], res)
            continue
        by_source[name] = len(res)
        if len(res) == 0:
            empty_sources.append(name)
        reviews.extend(res)

    # text_hash считает оркестратор централизованно (не коллекторы) —
    # единая нормализация для всех источников, ключ дедупликации и
    # UNIQUE-ограничения (миграция 081).
    for r in reviews:
        r["text_hash"] = text_hash(r["text"])

    cross_dupes = 0
    inserted = 0
    if reviews:
        before = len(reviews)
        reviews = dedupe_reviews(reviews)
        cross_dupes = before - len(reviews)
        for r in reviews:
            res = await execute("""
                INSERT INTO reviews_raw (
                    developer_id, complex_id, source, source_entity_id, author,
                    review_date, rating, review_text, text_hash, source_url, raw
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb)
                ON CONFLICT (complex_id, source, text_hash) DO NOTHING
            """, developer_id, cx["id"], r["source"], r.get("source_entity_id"),
                r.get("author"), r.get("review_date"), r.get("rating"),
                r["text"], r["text_hash"], r.get("source_url"),
                json.dumps(r.get("raw") or {}, ensure_ascii=False))
            inserted += 1 if res.endswith("1") else 0

    # Sentinel-строка «обработано, отзывов нет» (урок старого 2gis_
    # reviews_collect.py: без неё ЖК с успешным нулём отзывов
    # пересобирался бы КАЖДУЮ ночь вечно) — ТОЛЬКО для источников из
    # empty_sources (успешно ответили, 0 отзывов), И ТОЛЬКО если ВООБЩЕ
    # никто на этом ЖК не упал (failed_sources пуст). Иначе (например
    # 2gis упал, а google_maps/yandex — ПОСТОЯННЫЕ заглушки, всегда
    # "успешно" отвечающие []) sentinel'ы заглушек всё равно поставили
    # бы свежий fetched_at на ЖК -> _queue() посчитал бы его "недавно
    # проверенным" и НЕ вернул бы в очередь на ~25 дней, хотя РЕАЛЬНЫЙ
    # источник (2gis) так и не ответил успешно ни разу — тот же дефект
    # "ложный sentinel", просто через соседний источник, не напрямую.
    # Раз кто-то упал — весь ЖК остаётся в очереди без исключений.
    if not failed_sources:
        for name in empty_sources:
            await execute("""
                INSERT INTO reviews_raw (
                    developer_id, complex_id, source, review_text, text_hash,
                    raw, classified_at
                ) VALUES ($1, $2, $3, '', $4, '{"empty": true}'::jsonb, now())
                ON CONFLICT (complex_id, source, text_hash) DO NOTHING
            """, developer_id, cx["id"], name, text_hash(""))

    return {"complex_id": cx["id"], "by_source": by_source,
            "cross_dupes": cross_dupes, "inserted": inserted,
            "empty_sources": empty_sources, "failed_sources": failed_sources}


async def _queue(complex_id: int | None, developer_id: int | None,
                 limit: int) -> list[dict]:
    """Очередь ЖК к сбору: не мусорные, с координатами (координаты нужны
    будущим гео-матчерам источников) и без свежих строк в reviews_raw."""
    from bot.db.pg import fetch
    where = """
        WHERE COALESCE(c.is_garbage, FALSE) = FALSE
          AND COALESCE(c.is_street, FALSE) = FALSE
          AND c.lat IS NOT NULL AND c.lon IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM reviews_raw rr
                          WHERE rr.complex_id = c.id
                            AND rr.fetched_at > now() - ($1 || ' days')::interval)
    """
    params: list = [str(_REFRESH_DAYS)]
    if complex_id is not None:
        where += " AND c.id = $2"
        params.append(complex_id)
    elif developer_id is not None:
        where += " AND c.developer_id = $2"
        params.append(developer_id)
    params.append(limit)
    return await fetch(f"""
        SELECT c.id, c.name, c.developer_id FROM complexes c
        {where} ORDER BY c.id LIMIT ${len(params)}
    """, *params)


async def run(complex_id=None, developer_id=None, limit=50, fast=False) -> dict:
    sleep_s = _SLEEP_S_FAST if fast else _SLEEP_S
    queue = await _queue(complex_id, developer_id, limit)
    log.info("ЖК к обработке: %d (refresh-окно %d дн)", len(queue), _REFRESH_DAYS)

    stats = {"complexes": 0, "inserted": 0, "cross_dupes": 0, "by_source": {}}
    for cx in queue:
        cx = dict(cx)
        try:
            r = await collect_one_complex(cx, cx.get("developer_id"))
        except Exception as exc:
            log.warning("complex_id=%s %s — ошибка: %s", cx["id"], cx["name"][:40], exc)
            await asyncio.sleep(sleep_s)
            continue
        stats["complexes"] += 1
        stats["inserted"] += r["inserted"]
        stats["cross_dupes"] += r["cross_dupes"]
        for src, n in r["by_source"].items():
            stats["by_source"][src] = stats["by_source"].get(src, 0) + n
        log.info("complex_id=%s %s: %s, вставлено %d (кросс-дублей %d)",
                 cx["id"], cx["name"][:36], r["by_source"], r["inserted"], r["cross_dupes"])
        # Пауза СНАРУЖИ try/except (не внутри) — соблюдается на любом
        # исходе, урок 2gis_reviews_collect.py (см. его докстринг про
        # continue в try).
        if not fast:
            await asyncio.sleep(sleep_s)
    log.info("ИТОГ: %s", stats)
    return stats


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--complex-id", type=int, default=None)
    ap.add_argument("--developer-id", type=int, default=None)
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--fast", action="store_true", help="пауза 5с вместо 30с, без sleep между ЖК")
    args = ap.parse_args()

    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    try:
        result = await run(args.complex_id, args.developer_id, args.limit, args.fast)
        print(result)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
