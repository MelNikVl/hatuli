#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""audit_address_hash_exact.py — READ-ONLY аудит EXACT address_hash
(задача 2026-08-16, "безопасный deterministic exact-only property
linker", п.1) — до включения exact-only режима нужно честно ответить:
насколько сам exact-hash действительно идентифицирует ФИЗИЧЕСКУЮ
квартиру, а не просто "любую квартиру этого дома с такой площадью на
таком этаже".

НИЧЕГО НЕ ПИШЕТ — только SELECT.

Из чего строится address_hash (bot/identity/property_linker.py::
compute_address_hash, единственное место в проекте, где формула
определена — эта функция её ЧИТАЕТ, не дублирует):

    SHA1(normalize_address(address) + "|" + str(floor) + "|" + f"{area:.1f}")

  - normalize_address(address) — исходный текст apartment_listings.
    address (обычно "Улица, номер дома", иногда "— Название ЖК" через
    тире), в нижнем регистре, без административных шумовых слов
    (город/район/тип улицы — см. _ADDRESS_NOISE), схлопнутые пробелы.
    НОМЕР ДОМА ВХОДИТ (просто цифры, не в списке шумовых слов — ничего
    его не вырезает).
  - floor — apartment_listings.floor, целое число, ЭТАЖ ВХОДИТ.
  - area — apartment_listings.area, округлена до 0.1м², ПЛОЩАДЬ ВХОДИТ.

НЕ ВХОДИТ:
  - complex_id — НЕ участвует в хэше вовсе (только в fuzzy-ветке).
  - rooms — НЕ участвует.
  - apartment_number (номер квартиры/юнита) — В ПРИНЦИПЕ НЕ СУЩЕСТВУЕТ
    как отдельное поле нигде в схеме (нет такой колонки в apartment_
    listings — проверено \\d apartment_listings). Реальный address
    почти никогда не содержит номер квартиры текстом: из 50283 строк
    только 3 совпадают с грубым паттерном "кв. N" (см. отчёт).

ГЛАВНЫЙ РИСК (задача, явно просит оценку, не "гарантированно истинно"):
без apartment_number И без complex_id в хэше, ДВЕ РАЗНЫЕ квартиры одного
дома на ОДНОМ этаже с ОДИНАКОВОЙ (округлённой до 0.1м²) площадью
получат ИДЕНТИЧНЫЙ address_hash. Это не гипотетический край: у типового
панельного/монолитного дома на одном этаже часто 2-8 квартир, и
зеркальные/повторяющиеся планировки с одинаковой площадью — совсем не
редкость. address_hash НЕ ГАРАНТИРУЕТ идентификацию физической квартиры
— он идентифицирует "адрес+этаж+площадь", что для большинства домов с
одной квартирой на этаж (или уникальными площадями по этажу) совпадает
с квартирой, но не всегда.

Запуск:
    venv/bin/python scripts/audit_address_hash_exact.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.FileHandler("audit_address_hash_exact.log", encoding="utf-8", errors="replace")],
)
log = logging.getLogger("audit_address_hash_exact")

from bot.identity.property_linker import compute_address_hash
from seller_profile_snapshot import _normalize_name

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")


async def _load_rows() -> list[dict]:
    """Тот же набор полей, что scripts/audit_property_linker_fuzzy.py::
    _load_rows (дублируется НАМЕРЕННО, не импортируется оттуда — та
    ветка/PR ещё не смержена в master на момент этой задачи, независимая
    ветка не должна зависеть от неё)."""
    from bot.db.pg import fetch
    rows = await fetch("""
        SELECT id, address, floor, area, rooms, complex_name,
               first_seen, last_seen, archived_at, is_active,
               seller_name, price, lat, lon
        FROM apartment_listings
    """)
    return [dict(r) for r in rows]


def _active_overlap(a: dict, b: dict) -> bool | None:
    a_start, b_start = a.get("first_seen"), b.get("first_seen")
    if a_start is None or b_start is None:
        return None
    a_end = a.get("archived_at") or datetime.now(timezone.utc)
    b_end = b.get("archived_at") or datetime.now(timezone.utc)
    return a_start <= b_end and b_start <= a_end


def group_by_exact_hash(rows: list[dict]) -> dict[str, list[dict]]:
    by_hash: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        h = compute_address_hash(r.get("address"), r.get("floor"), r.get("area"))
        if h is not None:
            by_hash[h].append(r)
    return by_hash


def _cluster_report(h: str, members: list[dict]) -> dict:
    rooms_values = {m.get("rooms") for m in members if m.get("rooms") is not None}
    floor_values = {m.get("floor") for m in members}
    area_values = {round(m.get("area"), 2) for m in members if m.get("area") is not None}
    complex_values = {(m.get("complex_name") or "").strip().lower() for m in members
                       if m.get("complex_name")}

    sellers = [_normalize_name(m["seller_name"]) for m in members if m.get("seller_name")]
    distinct_sellers = len(set(sellers))

    has_simultaneous = False
    for i in range(len(members)):
        for j in range(i + 1, len(members)):
            if _active_overlap(members[i], members[j]) is True:
                has_simultaneous = True
                break
        if has_simultaneous:
            break

    return {
        "address_hash": h,
        "size": len(members),
        "listing_ids": [m["id"] for m in members],
        "rooms_differ": len(rooms_values) > 1,
        "rooms_values": sorted(v for v in rooms_values),
        "floor_differ": len(floor_values) > 1,  # структурно не должно случаться (floor часть хэша)
        "area_differ": len(area_values) > 1,    # округление 0.1 vs 0.01 может редко разойтись
        "complex_name_differ": len(complex_values) > 1,
        "distinct_seller_identities": distinct_sellers,
        "simultaneously_active": has_simultaneous,
    }


def audit_exact_clusters(rows: list[dict]) -> dict:
    by_hash = group_by_exact_hash(rows)
    multi = {h: members for h, members in by_hash.items() if len(members) > 1}

    reports = [_cluster_report(h, members) for h, members in multi.items()]

    size_dist: dict[str, int] = defaultdict(int)
    for r in reports:
        n = r["size"]
        if n == 2:
            size_dist["2"] += 1
        elif n <= 5:
            size_dist["3-5"] += 1
        elif n <= 10:
            size_dist["6-10"] += 1
        else:
            size_dist[">10"] += 1

    max_cluster = max((r["size"] for r in reports), default=0)
    simultaneously_active_count = sum(1 for r in reports if r["simultaneously_active"])
    different_seller_count = sum(1 for r in reports if r["distinct_seller_identities"] > 1)
    rooms_differ_count = sum(1 for r in reports if r["rooms_differ"])

    # Риск-скоринг НА КЛАСТЕР — та же честная эвристика, что fuzzy-аудит:
    # НЕ заявляем "это ошибка", просто явные сигналы.
    def _risk(r: dict) -> tuple[str, list[str]]:
        reasons = []
        high = medium = False
        if r["rooms_differ"]:
            high = True
            reasons.append(f"rooms различаются внутри одного address_hash: {r['rooms_values']}")
        if r["simultaneously_active"] and r["distinct_seller_identities"] > 1:
            high = True
            reasons.append("разные seller identity И объявления пересекались по активности")
        elif r["simultaneously_active"]:
            medium = True
            reasons.append("объявления пересекались по времени активности")
        elif r["distinct_seller_identities"] > 1:
            medium = True
            reasons.append("разные seller identity (без подтверждённого пересечения активности)")
        if r["size"] > 5:
            medium = True
            reasons.append(f"крупный кластер ({r['size']} listing) на одном address_hash")
        if high:
            return "high", reasons
        if medium:
            return "medium", reasons
        if not reasons:
            reasons.append("rooms совпадают (где известны), не пересекались по активности, "
                            "один seller identity или неизвестна")
        return "low", reasons

    for r in reports:
        r["risk"], r["risk_reasons"] = _risk(r)

    top50 = sorted(
        reports,
        key=lambda r: ({"high": 0, "medium": 1, "low": 2}[r["risk"]], -r["distinct_seller_identities"],
                        -r["size"], r["address_hash"]),
    )[:50]

    return {
        "total_listings": len(rows),
        "total_distinct_hashes": len(by_hash),
        "hashes_with_2plus_listings": len(multi),
        "cluster_size_distribution": dict(size_dist),
        "max_cluster_size": max_cluster,
        "simultaneously_active_clusters": simultaneously_active_count,
        "different_seller_identity_clusters": different_seller_count,
        "rooms_differ_clusters": rooms_differ_count,
        "top50_suspicious_exact_clusters": top50,
        "risk_assessment": (
            "address_hash НЕ содержит apartment_number (поля не существует в схеме, "
            "в реальных адресах практически не встречается — 3/50283) и НЕ содержит "
            "complex_id/rooms. Совпадение хэша означает 'тот же адрес+этаж+площадь(±0.05м²)', "
            "НЕ гарантированно 'та же физическая квартира' — на одном этаже дома с "
            "несколькими одинаковыми по площади юнитами (частая планировка) exact match "
            "МОЖЕТ склеить две разные квартиры. rooms_differ_clusters "
            f"({rooms_differ_count}) — САМОЕ прямое структурное доказательство: rooms "
            "не входит в хэш, поэтому такие случаи технически возможны, и raw-факт их "
            "наличия НАПРЯМУЮ опровергает 'exact hash = гарантированно одна квартира'."
        ),
    }


async def main() -> None:
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    try:
        rows = await _load_rows()
        result = audit_exact_clusters(rows)
        summary = {k: v for k, v in result.items() if k != "top50_suspicious_exact_clusters"}
        summary["top5_suspicious_preview"] = result["top50_suspicious_exact_clusters"][:5]
        log.info("ИТОГ: %s", json.dumps(summary, ensure_ascii=False, default=str))
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
        with open("audit_address_hash_exact_top50.log", "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
