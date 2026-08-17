#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/audit_orphan_properties.py — задача 2026-08-17, "Missing floor +
orphan audit", п.2: аудит properties без единой строки в property_listings
("предполагаемые 17 properties" — на проде на момент написания РОВНО 17,
проверено прямым запросом, не вычитанием count'ов, см. ниже).

READ-ONLY. Ничего не удаляет, ничего не пишет. Прямой NOT EXISTS-запрос
(не COUNT(properties) - COUNT(DISTINCT property_listings.property_id) —
задача явно требует именно прямой запрос: разность двух count'ов не может
показать, КАКИЕ именно properties осиротели, только сколько).

## Для каждой orphan property показываем

  - property_id, complex_id;
  - created_at / first_seen_at / last_seen_at;
  - address_hash;
  - есть ли ссылки из property_match_candidates (кто-то ДРУГОЙ считает эту
    property кандидатом — независимое подтверждение, что хэш/complex/floor/
    area когда-то были реальным сигналом, не мусором);
  - возможный исходный listing — ДВА уровня поиска:
      1) точное совпадение (complex_id, floor, area_sqm) СРЕДИ ТЕКУЩИХ
         apartment_listings + пересчитанный address_hash действительно
         совпадает с property.address_hash (bot.identity.property_linker.
         compute_address_hash — единственное место, где считается формула,
         эта функция ИМПОРТИРУЕТСЯ, не дублируется);
      2) если (1) пуст — ПОЛНЫЙ скан apartment_listings (все id/address/
         floor/area) с пересчётом хэша для КАЖДОГО текущего listing,
         независимо от текущих floor/area (ловит случай "у исходного
         listing'а floor/area с тех пор поменялись на другое значение" —
         этот случай (1) не поймал бы, т.к. фильтрует по ТЕКУЩИМ floor/area);
  - вероятная причина (см. _probable_cause) — структурная гипотеза по
    FK/каскадам, НЕ утверждение "это точно случилось так" без прямого лога
    удалений (такого лога в проекте нет).

## Найденная (эмпирически, на реальных данных) вероятная причина

property_listings.listing_id REFERENCES apartment_listings(id) ON DELETE
CASCADE (проверено: SELECT confdeltype FROM pg_constraint — 'c'). properties
НЕ каскадируется от apartment_listings вообще (она родитель, не потомок).
bot/core/archive_check.py делает `DELETE FROM apartment_listings WHERE id =
$1`, когда страница объявления подтверждённо пропала (404/410/"В архиве") —
это ЖИВОЙ, постоянно бегущий код (часть service_apartments.run_cycle,
таймер krisha-apartments.service), не разовый скрипт. Если listing X был
единственным listing'ом своей property (bootstrap создал property+
property_listings ИЗ X), а потом X удалили archive_check'ом — property_
listings-строка X каскадом исчезает, properties-строка остаётся: ровно тот
паттерн, что видим у 15 из 17 orphan'ов на проде (полный хэш-скан НЕ находит
ни одного текущего listing с их address_hash — согласуется с "исходный
listing удалён", хотя без лога удалений это гипотеза, не доказательство).
У 2 из 17 (см. отчёт задачи) хэш-скан находит листинг с ТЕМ ЖЕ хэшем, но
тот листинг уже связан с ДРУГОЙ property — совпадение хэша двух разных
физических объектов (одинаковый адрес+этаж+площадь, см. migrations/086 —
UNIQUE(address_hash) снят именно из-за этого), не тот же случай.

## Отдельная гипотеза — race в bootstrap INSERT

bootstrap_all_provisional()/_link_candidate_only() (bot/identity/
property_linker.py) делают INSERT INTO properties, ЗАТЕМ отдельным
execute() INSERT INTO property_listings — БЕЗ общей транзакции. Если
процесс упадёт/будет убит МЕЖДУ этими двумя INSERT (или второй INSERT
провалится по любой другой причине) — тот же симптом: property есть,
property_listings нет. Мы НЕ можем отличить эту причину от cascade-delete
задним числом по данным by этого аудита (обе дают "нет текущего listing'а с
таким хэшем" ЕСЛИ второй INSERT просто не случился для listing'а, который
ПОТОМ ещё и не переобработался). Обе причины реальны и не взаимоисключающие
— задача просит предложить транзакционный фикс ИМЕННО для этого случая
(атомарный INSERT properties + INSERT property_listings), см. ФИКС ниже.
Фикс cascade-delete-причины — ДРУГОЙ (properties не должна становиться
неявным сиротой при удалении своего единственного listing'а — например,
soft-delete apartment_listings вместо hard DELETE, или триггер, помечающий
identity_status='orphaned' при последнем cascade) — вне контекста одной
транзакции INSERT, сознательно НЕ в этом PR.

## ПРЕДЛОЖЕНИЕ ФИКСА (задача: "предложить отдельным коммитом", НЕ
применять сейчас, никаких данных не чистить без ОК)

Обернуть INSERT INTO properties + INSERT INTO property_listings в
bootstrap_all_provisional()/_link_candidate_only()/link_listing_to_property()
в одну транзакцию (bot/db/pg.py::get_pool().acquire() + conn.transaction()
— тот же паттерн, что _acquire_lock() в property_identity_incremental.py
уже использует для advisory lock, только здесь ради атомарности, не
блокировки). Это устраняет race-причину; cascade-delete-причину НЕ трогает
(отдельная гипотеза, отдельный будущий PR — см. выше). Не делаю это сейчас:
(а) требует отдельного code review самого горячего пути линковщика,
(б) данные уже осиротевшие этим фиксом НЕ починятся (нужен отдельный
data-fix после моего ОК, задача явно это требует).

Запуск: venv/bin/python scripts/audit_orphan_properties.py [--json]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

# scripts/ — sys.path[0] по умолчанию = сам этот каталог, "from bot...."
# не резолвится без явного добавления корня репозитория (тот же приём,
# что scripts/backfill_property_ids.py).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")


async def _orphan_properties() -> list[dict]:
    """Прямой NOT EXISTS — задача, явно: "не выводить только вычитанием
    counts"."""
    from bot.db.pg import fetch
    rows = await fetch("""
        SELECT p.property_id, p.complex_id, p.address_hash, p.floor, p.area_sqm,
               p.rooms, p.identity_status, p.created_at, p.first_seen_at, p.last_seen_at
        FROM properties p
        WHERE NOT EXISTS (SELECT 1 FROM property_listings pl WHERE pl.property_id = p.property_id)
        ORDER BY p.property_id
    """)
    return [dict(r) for r in rows]


async def _candidate_refs(property_id: int) -> list[dict]:
    """Кто ДРУГОЙ (живой) listing ссылается на эту orphan property как на
    кандидата — независимое подтверждение реальности сигнала."""
    from bot.db.pg import fetch
    rows = await fetch("""
        SELECT candidate_id, listing_id, match_method, status, match_score
        FROM property_match_candidates WHERE candidate_property_id = $1
        ORDER BY candidate_id
    """, property_id)
    return [dict(r) for r in rows]


async def _possible_source_listing(prop: dict) -> dict:
    """Уровень 1 (текущие floor/area совпадают) -> уровень 2 (полный
    пересчёт хэша по ВСЕЙ таблице, независимо от текущих floor/area —
    ловит случай "поля с тех пор изменились"). Возвращает {"level": 0|1|2,
    "listings": [...]}; level=0 — ничего не нашлось ни на одном уровне."""
    from bot.db.pg import fetch
    from bot.identity.property_linker import compute_address_hash

    candidates = await fetch("""
        SELECT al.id, al.address, al.floor, al.area, al.complex_name, al.first_seen, al.last_seen
        FROM apartment_listings al
        LEFT JOIN complexes c ON lower(trim(c.name)) = lower(trim(al.complex_name))
        WHERE (c.id = $1 OR ($1 IS NULL AND al.complex_name IS NULL))
          AND al.floor = $2 AND al.area = $3
    """, prop["complex_id"], prop["floor"], prop["area_sqm"])
    level1 = [dict(c) for c in candidates
              if compute_address_hash(c["address"], c["floor"], c["area"]) == prop["address_hash"]]
    if level1:
        return {"level": 1, "listings": level1}

    # Уровень 2 — дорогой (пересчёт хэша по ВСЕЙ таблице), поэтому только
    # если уровень 1 пуст. На проде (51к строк) — секунды, не минуты,
    # приемлемо для разового аудита 17 строк.
    all_listings = await fetch("SELECT id, address, floor, area FROM apartment_listings")
    level2 = [dict(r) for r in all_listings
              if compute_address_hash(r["address"], r["floor"], r["area"]) == prop["address_hash"]]
    if level2:
        return {"level": 2, "listings": level2}
    return {"level": 0, "listings": []}


async def _has_property_listings(listing_id: str) -> int | None:
    from bot.db.pg import fetchval
    return await fetchval("SELECT property_id FROM property_listings WHERE listing_id = $1", listing_id)


def _probable_cause(candidate_refs: list[dict], source: dict) -> str:
    if source["level"] == 0:
        return ("cascade_delete_likely: ни один текущий apartment_listings не пересчитывает в этот "
                "address_hash (ни по текущим floor/area, ни по полному скану) — согласуется с тем, что "
                "исходный listing (единственный источник этой property) был позже удалён "
                "(bot/core/archive_check.py, DELETE FROM apartment_listings ON confirmed-gone), что "
                "каскадом убрало property_listings, но НЕ properties. Альтернатива — race в bootstrap "
                "INSERT (см. докстринг модуля), неотличима от cascade-delete по одним этим данным.")
    if source["level"] in (1, 2):
        return ("hash_collision_with_live_listing: текущий listing с тем же address_hash существует, "
                "но он уже связан с ДРУГОЙ property — совпадение адрес+этаж+площадь двух разных "
                "физических объектов (UNIQUE(address_hash) снят миграцией 086 именно из-за этого класса "
                "случаев), эта orphan property НЕ является 'потерянной' версией найденного listing'а.")
    return "unknown"


async def run_audit() -> list[dict]:
    orphans = await _orphan_properties()
    report = []
    for p in orphans:
        refs = await _candidate_refs(p["property_id"])
        source = await _possible_source_listing(p)
        for l in source["listings"]:
            l["currently_linked_to_property_id"] = await _has_property_listings(l["id"])
        report.append({
            "property_id": p["property_id"],
            "complex_id": p["complex_id"],
            "address_hash": p["address_hash"],
            "floor": p["floor"],
            "area_sqm": p["area_sqm"],
            "rooms": p["rooms"],
            "identity_status": p["identity_status"],
            "created_at": p["created_at"].isoformat() if p["created_at"] else None,
            "first_seen_at": p["first_seen_at"].isoformat() if p["first_seen_at"] else None,
            "last_seen_at": p["last_seen_at"].isoformat() if p["last_seen_at"] else None,
            "candidate_references": refs,
            "possible_source_listing": source,
            "probable_cause": _probable_cause(refs, source),
        })
    return report


def _print_human(report: list[dict]) -> None:
    print(f"\n=== orphan properties: {len(report)} (прямой NOT EXISTS-запрос) ===\n")
    for r in report:
        print(f"property_id={r['property_id']}  complex_id={r['complex_id']}  "
              f"floor={r['floor']}  area={r['area_sqm']}  rooms={r['rooms']}")
        print(f"  address_hash={r['address_hash']}")
        print(f"  created_at={r['created_at']}  first_seen_at={r['first_seen_at']}  "
              f"last_seen_at={r['last_seen_at']}")
        print(f"  candidate_references: {len(r['candidate_references'])}"
              f"{' -> ' + str(r['candidate_references']) if r['candidate_references'] else ''}")
        src = r["possible_source_listing"]
        print(f"  possible_source_listing: level={src['level']} listings={src['listings']}")
        print(f"  probable_cause: {r['probable_cause']}")
        print()
    by_cause: dict[str, int] = {}
    for r in report:
        key = r["probable_cause"].split(":")[0]
        by_cause[key] = by_cause.get(key, 0) + 1
    print(f"=== по причине: {by_cause} ===\n")


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="машиночитаемый вывод вместо человеческого")
    args = ap.parse_args()

    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    try:
        report = await run_audit()
    finally:
        await close_pool()

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        _print_human(report)


if __name__ == "__main__":
    asyncio.run(main())
