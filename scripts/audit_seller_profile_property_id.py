#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""audit_seller_profile_property_id.py — READ-ONLY аудит: насколько
property_id (миграции 083/084, bot/identity/property_linker.py, задача
2026-08-16 "P1 — Property Identity") меняет портрет продавца из
seller_profiles (seller_profile_snapshot.py, §2.7 liquidity_model_
design.md), ДО того как что-либо в проде на этом менять.

НИЧЕГО НЕ ПИШЕТ: ни в seller_profiles, ни в какую-либо другую таблицу —
только SELECT. Явно НЕ трогает seller_profile_snapshot.py/схему/UI/
действующие метрики (задача 2026-08-16, "read-only аудит Seller Profile
на базе property_id").

Зачем отдельный скрипт, а не правка seller_profile_snapshot.py: одна и
та же физическая квартира сейчас = N разных listing_id (relist), и
seller_profiles.total_listings_count/relist_count считают её N раз —
насколько это меняет картину, если считать по property_id вместо
listing_id, НЕИЗВЕСТНО до этого аудита. Правка снапшота вслепую до
измерения — риск незаметно сломать действующие продуктовые метрики.

ВАЖНОЕ ОГРАНИЧЕНИЕ ДАННЫХ (см. итоговый отчёт): scripts/backfill_
property_ids.py на реальных данных НЕ запускался (явное условие
пользователя из задачи "P1 — Property Identity" — "НЕ запускать backfill
на реальных данных без моего ОК", согласия так и не было получено) —
property_listings на проде ПУСТА. Этот аудит корректно это отражает
(coverage_ratio≈0, всё в бакете "без property_id"), но НЕ может показать
реальную картину "как property_id меняет портрет продавца", пока
backfill не выполнен. Гейт "--sample 100 на реальной БД" всё равно
выполним и осмыслен: он показывает ИМЕННО это состояние (0% покрытия) —
само по себе диагностическая находка, не пустышка.

Идентификатор продавца — та же нормализация seller_name (trim+lower+
схлопнутые пробелы) и тот же стоп-лист generic-имён ("хозяин"/
"продавец"/...), что seller_profile_snapshot.py — импортируются оттуда
напрямую (_normalize_name/_GENERIC_NAME_STOPLIST/_AMBIGUOUS_NAME_MIN_
LISTINGS), не переопределяются заново: единый источник правды, иначе
"старая" и "новая" метрики считались бы на РАЗНЫХ группировках и
сравнение было бы нечестным.

Метрики на КАЖДУЮ seller identity (см. _audit_seller()):
  1. listing_count               — старая метрика (= total_listings_count
                                    в seller_profiles при свежем снапшоте)
  2. unique_property_count       — COUNT(DISTINCT property_id)
  3. property_id_coverage        — with/without property_id + coverage_ratio
  4. property_relist_count       — доп. listing_id той же квартиры У ЭТОЙ
                                    ЖЕ identity (см. докстринг _audit_seller
                                    — НЕ "перепродажа"/"сделка")
  5. repeated_property_ratio     — доля properties с >1 listing_id
  6. observed_span_*             — MIN/MAX по property, РАЗДЕЛЬНО для
                                    active/censored и concluded (см.
                                    докстринг — НЕ true_dom)
  7. complex_diversity_count     — уникальных complex_id среди properties
  8. is_ambiguous_recomputed/_stored/ambiguity_source_matches,
     completeness_ratio          — см. докстринг _audit_seller()

Запуск:
    venv/bin/python scripts/audit_seller_profile_property_id.py --sample 100
    venv/bin/python scripts/audit_seller_profile_property_id.py          # вся популяция
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone

# scripts/ — не корень репо, тот же приём, что остальные scripts/*.py
# этой ветки задач (backfill_property_ids.py, sync_city_poi.py).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv()

# Единый источник правды для нормализации/стоп-листа/порога ambiguous —
# см. докстринг модуля выше про "нечестное сравнение", если бы это было
# продублировано здесь заново.
from seller_profile_snapshot import _normalize_name, _GENERIC_NAME_STOPLIST, _AMBIGUOUS_NAME_MIN_LISTINGS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.FileHandler("audit_seller_profile_property_id.log", encoding="utf-8", errors="replace")],
)
log = logging.getLogger("audit_seller_profile_property_id")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

# Задача, п.8 "completeness считать по явно перечисленным полям" —
# явный список, не "все столбцы properties" (address_hash/created_at и
# т.п. не про качество данных для потребителя, id/дедуп-техника).
COMPLETENESS_FIELDS = ("complex_id", "floor", "area_sqm", "rooms")


async def _load_rows() -> list[dict]:
    """Одна строка на (listing, его property, если есть) — тот же паттерн
    полного прохода + группировка в Python, что seller_profile_snapshot.py
    (небольшое число СУЩНОСТЕЙ, дешевле одного прохода, чем запрос на
    каждого продавца). LEFT JOIN — listing без property_id (backfill не
    запускался/квартира ещё не привязана) ОБЯЗАН остаться в выборке
    (задача: "объявления без property_id не исключать молча") — p.*
    будут NULL, это и есть honest "без property_id"."""
    from bot.db.pg import fetch
    rows = await fetch("""
        SELECT al.id AS listing_id, al.seller_name, al.is_active,
               pl.property_id,
               p.complex_id, p.floor, p.area_sqm, p.rooms,
               p.first_seen_at AS property_first_seen_at,
               p.last_seen_at AS property_last_seen_at
        FROM apartment_listings al
        LEFT JOIN property_listings pl ON pl.listing_id = al.id
        LEFT JOIN properties p ON p.property_id = pl.property_id
        WHERE al.seller_name IS NOT NULL AND btrim(al.seller_name) != ''
    """)
    return [dict(r) for r in rows]


async def _load_seller_profiles_ambiguity() -> dict[str, dict]:
    """seller_name (уже нормализован — PK seller_profiles) -> {is_ambiguous,
    total_listings_count} — задача п.8: "сначала проверить фактическую
    схему и место хранения is_ambiguous". Схема проверена (миграция 079):
    это РЕАЛЬНЫЙ столбец seller_profiles, посчитанный seller_profile_
    snapshot.py::_aggregate() как total_listings_count > 15 НА МОМЕНТ
    последнего снапшота — может быть stale относительно этого аудита
    (другая точка во времени). Аудит НЕ переиспользует его как источник
    истины напрямую — пересчитывает is_ambiguous_recomputed заново по
    ТОЙ ЖЕ формуле из СВЕЖИХ данных (единый источник — тот же _AMBIGUOUS_
    NAME_MIN_LISTINGS) и лишь СВЕРЯЕТ со stored (см. _audit_seller —
    ambiguity_source_matches, "связь корректна" только когда строка
    seller_profiles реально найдена по этому seller_name)."""
    from bot.db.pg import fetch
    rows = await fetch("SELECT seller_name, is_ambiguous, total_listings_count FROM seller_profiles")
    return {r["seller_name"]: dict(r) for r in rows}


def _group_by_seller(rows: list[dict]) -> dict[str, list[dict]]:
    """Та же нормализация/стоп-лист, что seller_profile_snapshot.py —
    единая группировка, честное old vs new сравнение (см. докстринг
    модуля)."""
    by_seller: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        name_norm = _normalize_name(r["seller_name"])
        if name_norm in _GENERIC_NAME_STOPLIST:
            continue
        by_seller[name_norm].append(r)
    return by_seller


def _audit_seller(name_norm: str, group: list[dict],
                   seller_profiles_by_name: dict[str, dict]) -> dict:
    """Все метрики задачи для ОДНОЙ seller identity — см. докстринг
    модуля, пункты 1-8. `group` — ТОЛЬКО listing'и этой identity (см.
    _group_by_seller) — структурная гарантия п.4 "не присваивать
    продавцу релисты, опубликованные другой seller identity": группировка
    по property_id ниже строится ВНУТРИ уже отфильтрованного group,
    listing чужой identity с тем же property_id физически не может
    попасть в этот словарь."""
    listing_count = len(group)

    with_pid = [r for r in group if r.get("property_id") is not None]
    listings_with_property_id = len(with_pid)
    listings_without_property_id = listing_count - listings_with_property_id
    coverage_ratio = round(listings_with_property_id / listing_count, 4) if listing_count else None

    by_property: dict[int, list[dict]] = defaultdict(list)
    for r in with_pid:
        by_property[r["property_id"]].append(r)
    unique_property_count = len(by_property)

    # 4. property_relist_count — СТРУКТУРНЫЙ факт "N-1 доп. listing_id
    # у той же физической квартиры этой identity", НЕ вывод о сделке:
    # новый listing_id может быть тем же объявлением, republished ботом
    # продавца, ошибкой парсинга адреса и т.п. — этот аудит не проверяет
    # причину, только считает факт совпадения property_id (задача,
    # п.4: "новый listing_id не называть перепродажей или совершённой
    # сделкой").
    property_relist_count = sum(len(v) - 1 for v in by_property.values())

    # 5. repeated_property_ratio
    repeated_property_count = sum(1 for v in by_property.values() if len(v) > 1)
    repeated_property_ratio = (round(repeated_property_count / unique_property_count, 4)
                                if unique_property_count else None)

    # 6. observed_property_span_days — ПО КАЖДОЙ уникальной property, ИЗ
    # properties.first_seen_at/last_seen_at (уже MIN/MAX по всем её
    # listing'ам, см. bot/identity/property_linker.py). Явно НЕ "true
    # DOM": last_seen_at — "когда В ПОСЛЕДНИЙ РАЗ видели ХОТЬ ОДИН
    # listing этой квартиры", НЕ дата продажи/снятия — интервалы, когда
    # квартира вообще не была выставлена (между relist'ами), в property_
    # id слое пока не восстановлены. active_censored/concluded считаются
    # РАЗДЕЛЬНО (задача, п.6: "не подставлять NOW() в обычное среднее
    # без маркировки") — для censored last_seen_at это фактически "по
    # состоянию на последний скан", НЕ финальная дата, для concluded —
    # честная финальная дата (ни один listing больше не активен).
    concluded_spans: list[int] = []
    censored_spans: list[int] = []
    for pid, rs in by_property.items():
        first_at = rs[0].get("property_first_seen_at")
        last_at = rs[0].get("property_last_seen_at")
        if first_at is None or last_at is None:
            continue
        span_days = (last_at - first_at).days
        is_censored = any(r.get("is_active") is not False for r in rs)
        (censored_spans if is_censored else concluded_spans).append(span_days)

    # 7. complex_diversity
    complex_ids = {r["complex_id"] for r in with_pid if r.get("complex_id") is not None}
    complex_diversity_count = len(complex_ids)

    # 8a. ambiguity — см. докстринг _load_seller_profiles_ambiguity()
    is_ambiguous_recomputed = listing_count > _AMBIGUOUS_NAME_MIN_LISTINGS
    stored = seller_profiles_by_name.get(name_norm)
    is_ambiguous_stored = stored["is_ambiguous"] if stored is not None else None
    ambiguity_source_matches = (
        (is_ambiguous_recomputed == is_ambiguous_stored) if stored is not None else None
    )

    # 8b. completeness — явно перечисленные COMPLETENESS_FIELDS,
    # знаменатель = unique_property_count * len(COMPLETENESS_FIELDS),
    # выводится явно (задача: "вывести знаменатель каждой метрики").
    filled = 0
    for pid, rs in by_property.items():
        r0 = rs[0]
        filled += sum(1 for f in COMPLETENESS_FIELDS if r0.get(f) is not None)
    completeness_denominator = unique_property_count * len(COMPLETENESS_FIELDS)
    completeness_ratio = round(filled / completeness_denominator, 4) if completeness_denominator else None

    return {
        "seller_name": name_norm,
        "listing_count": listing_count,
        "unique_property_count": unique_property_count,
        "listings_with_property_id": listings_with_property_id,
        "listings_without_property_id": listings_without_property_id,
        "coverage_ratio": coverage_ratio,
        "coverage_denominator": listing_count,
        "property_relist_count": property_relist_count,
        "repeated_property_count": repeated_property_count,
        "repeated_property_ratio": repeated_property_ratio,
        "repeated_property_denominator": unique_property_count,
        "observed_span_concluded_count": len(concluded_spans),
        "observed_span_concluded_mean_days": (
            round(statistics.mean(concluded_spans), 1) if concluded_spans else None),
        "observed_span_active_censored_count": len(censored_spans),
        "observed_span_active_censored_mean_days": (
            round(statistics.mean(censored_spans), 1) if censored_spans else None),
        "complex_diversity_count": complex_diversity_count,
        "complex_diversity_denominator": unique_property_count,
        "is_ambiguous_recomputed": is_ambiguous_recomputed,
        "is_ambiguous_stored": is_ambiguous_stored,
        "ambiguity_source_matches": ambiguity_source_matches,
        "completeness_ratio": completeness_ratio,
        "completeness_filled": filled,
        "completeness_denominator": completeness_denominator,
    }


def _select_sample(by_seller: dict[str, list[dict]], sample: int | None) -> dict[str, list[dict]]:
    """Детерминированная выборка — ORDER BY listing_count DESC, seller_name
    ASC (полный тай-брейк, стабильно между прогонами: обычный порядок
    dict.items() в Python как раз стабилен по insertion order, но
    insertion order сам зависит от порядка строк из БД — не гарантирован
    без явного ORDER BY, поэтому сортируем явно). Приоритет САМЫМ
    ОБЪЁМНЫМ identity — там, где relist/property_id эффект (если он
    вообще есть при 0% coverage, см. докстринг модуля) виднее всего.
    None -> вся популяция, без ограничения."""
    if sample is None:
        return by_seller
    ordered = sorted(by_seller.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    return dict(ordered[:sample])


def _summarize(audits: list[dict]) -> dict:
    """Агрегированный отчёт: старое/новое, abs/% diff, распределение по
    unique_property_count (СТРУКТУРНЫЕ бакеты, БЕЗ owner/realtor —
    задача явно это запрещает без доп. подтверждения), top-20
    расхождений, агрегированный coverage, сверка ambiguity."""
    total_sellers = len(audits)
    total_listings = sum(a["listing_count"] for a in audits)
    total_unique_properties = sum(a["unique_property_count"] for a in audits)
    total_with_pid = sum(a["listings_with_property_id"] for a in audits)
    total_without_pid = sum(a["listings_without_property_id"] for a in audits)

    diff_abs = total_unique_properties - total_listings
    diff_pct = round(100 * diff_abs / total_listings, 2) if total_listings else None

    buckets = {"1": 0, "2-5": 0, "6-20": 0, ">20": 0}
    for a in audits:
        n = a["unique_property_count"]
        if n <= 1:
            buckets["1"] += 1
        elif n <= 5:
            buckets["2-5"] += 1
        elif n <= 20:
            buckets["6-20"] += 1
        else:
            buckets[">20"] += 1

    top20 = sorted(
        audits, key=lambda a: abs(a["listing_count"] - a["unique_property_count"]), reverse=True
    )[:20]

    ambiguity_checked = [a for a in audits if a["ambiguity_source_matches"] is not None]
    ambiguity_mismatches = [a for a in ambiguity_checked if not a["ambiguity_source_matches"]]

    return {
        "sellers_audited": total_sellers,
        "old_total_listing_count": total_listings,
        "new_total_unique_property_count": total_unique_properties,
        "diff_abs": diff_abs,
        "diff_pct": diff_pct,
        "property_id_coverage": {
            "listings_with_property_id": total_with_pid,
            "listings_without_property_id": total_without_pid,
            "coverage_ratio": round(total_with_pid / total_listings, 4) if total_listings else None,
            "denominator": total_listings,
        },
        # НЕ owner/realtor — структурные бакеты по числу unique properties
        # (задача, сравнение: "НЕ называть эти группы owner/realtor без
        # дополнительного подтверждения").
        "unique_property_count_distribution": buckets,
        "top20_largest_discrepancies": [
            {"seller_name": a["seller_name"], "listing_count": a["listing_count"],
             "unique_property_count": a["unique_property_count"],
             "diff_abs": a["listing_count"] - a["unique_property_count"]}
            for a in top20
        ],
        "ambiguity_cross_check": {
            "checked": len(ambiguity_checked), "mismatches": len(ambiguity_mismatches),
            "denominator": len(ambiguity_checked),
        },
    }


async def run_audit(sample: int | None = None) -> dict:
    """Единственная функция, которая трогает БД — ТОЛЬКО SELECT (_load_
    rows/_load_seller_profiles_ambiguity), ни одного execute()/INSERT/
    UPDATE/DELETE во всём модуле (см. tests/test_seller_profile_
    property_id_audit.py::test_module_has_no_write_sql — структурная
    проверка read-only гарантии)."""
    rows = await _load_rows()
    seller_profiles_by_name = await _load_seller_profiles_ambiguity()
    by_seller = _select_sample(_group_by_seller(rows), sample)
    audits = [_audit_seller(name, group, seller_profiles_by_name)
              for name, group in by_seller.items()]
    return {"per_seller": audits, "summary": _summarize(audits)}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=None,
                     help="ограничить N крупнейшими seller identity (детерминированно, см. _select_sample)")
    args = ap.parse_args()

    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    try:
        result = await run_audit(sample=args.sample)
        log.info("ИТОГ: %s", result["summary"])
        print(result["summary"])
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
