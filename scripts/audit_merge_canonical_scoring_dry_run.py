#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/audit_merge_canonical_scoring_dry_run.py — задача 2026-08-18,
follow-up "Property Identity — calibration validation", п.6: заменяет
простое правило "активнее сейчас -> раньше first_seen -> меньший id"
(docs/property_merge_design.md §1, первая версия) на многофакторный
deterministic scoring, и печатает ПОЛНЫЙ read-only dry-run на реальной
самой длинной цепочке accepted-решений. НИЧЕГО не пишет, физический
merge НЕ выполняется — этот файл только СЧИТАЕТ и ПЕЧАТАЕТ план.

## Почему многофакторный scoring, не три правила

Задача, явно: "простого правила недостаточно". Три старых правила
теряют информацию — property с 1 активным листингом, но 10-летней
историей и полными атрибутами может быть содержательно надёжнее
"якоря", чем property с 2 активными листингами, но без этажа/координат
и однодневной историей. Ниже — взвешенная сумма нормализованных
0..1 суб-баллов на факторах, которые перечислила задача:

  completeness (25%)        — доля заполненных complex_id/floor/
                               area_sqm/rooms (0..1, 4 поля)
  address_consistency (15%) — согласие ТЕКУЩИХ (не замороженных на
                               bootstrap) адресов связанных listing'ов
                               между собой: 1.0, если все совпадают
  coords_presence (10%)     — есть ли хоть один связанный listing с
                               lat/lon (apartment_listings, НЕ properties
                               — координаты живут на листинге)
  history_duration (15%)    — (last_seen_at-first_seen_at), нормализовано
                               ОТНОСИТЕЛЬНО компоненты (самая долгая
                               история в группе = 1.0)
  listing_count (15%)       — count(property_listings), нормализовано
                               относительно компоненты
  conflict_absence (10%)    — 1/(1+n) конфликтных candidate-строк,
                               касающихся этой property (0 конфликтов = 1.0)
  freshness (10%)           — recency last_seen_at, нормализовано
                               относительно компоненты (самый свежий = 1.0)

Веса — ЭКСПЕРТНЫЕ, НЕ откалиброванные на исходе реальных merge (тот же
честный disclaimer, что docs/location_score_calibration_audit.md §2 —
не выдаю их за измеренную истину). Стабильный tie-break — меньший
property_id (последняя строка сортировки, детерминированно).

## Что печатает dry-run на каждую компоненту

- итоговый score каждой property (полный breakdown по суб-баллам);
- выбранный canonical + обоснование (не просто "он выиграл", а какие
  суб-баллы были решающими);
- какие property_listings были бы репойнтнуты (listing_id -> откуда куда);
- конфликты атрибутов между canonical и losing (разные floor/area/rooms/
  complex_id — если ТАКИЕ пары вообще существуют, это сигнал, что merge
  ДОЛЖЕН был бы получить ручное предупреждение, не тихий auto-merge);
- как выглядел бы rollback (moved_listing_ids snapshot).

Полная таблица — на ВСЕ компоненты (сейчас: см. вывод). Развёрнутый
per-listing dry-run — только на САМУЮ длинную цепочку (задача, явно:
"для реальной цепочки из 15 properties").

## Обновление 2026-08-20 ("Safe Physical Property Merge")

Формула (build_components/_load_property_facts/score_canonical_
candidates) ПЕРЕЕХАЛА в bot/identity/property_merge.py — production-код
merge engine'а. Этот скрипт больше НЕ держит свою копию (задача явно:
"не изобретай вторую систему") — импортирует ровно ту же реализацию,
только печатает. Единственное отличие вывода: score_canonical_
candidates() теперь дополнительно сортирует по identity_status-тиру
ПЕРЕД 7-факторным score (bot/identity/property_merge.py докстринг,
"Расхождение 1") — на сегодняшних данных (100% properties 'provisional')
это НЕ меняет ни одного результата этого скрипта.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

from bot.identity.property_merge import (  # noqa: E402  (после sys.path.insert)
    _CANONICAL_WEIGHTS as _WEIGHTS,
    build_components as _build_components,
    score_canonical_candidates as _score_component,
    _load_component_facts as _load_property_facts,
)


async def main() -> None:
    from bot.db.pg import init_pool, close_pool, fetch

    await init_pool(DATABASE_URL)
    try:
        edges = await fetch("""
            SELECT pl.property_id AS prop_a, pmc.candidate_property_id AS prop_b
            FROM property_match_candidates pmc
            JOIN property_listings pl ON pl.listing_id = pmc.listing_id
            WHERE pmc.status = 'accepted'
        """)
        components = _build_components(edges)
        all_prop_ids = [p for members in components.values() for p in members]
        facts = await _load_property_facts(all_prop_ids)
    finally:
        await close_pool()

    print(f"компонент: {len(components)}, properties: {len(all_prop_ids)}")
    sizes = sorted((len(m) for m in components.values()), reverse=True)
    print(f"размеры (топ 10): {sizes[:10]}")

    biggest_key = max(components, key=lambda k: len(components[k]))
    biggest = components[biggest_key]
    print(f"\n=== Самая длинная цепочка: {len(biggest)} properties: {sorted(biggest)} ===")

    scored = _score_component(biggest, facts)
    print("\n-- Scoring (отсортировано по убыванию, canonical = первая строка) --")
    for s in scored:
        print(f"  property_id={s['property_id']:>6}  score={s['score']:.4f}  "
              f"listings={s['n_listings']}  conflicts={s['n_conflicts']}  "
              f"floor={s['floor']}  area={s['area_sqm']}  rooms={s['rooms']}  "
              f"complex_id={s['complex_id']}  first_seen={s['first_seen_at'][:10]}  "
              f"last_seen={s['last_seen_at'][:10]}")
        print(f"      subscores: {s['subscores']}")

    canonical = scored[0]
    losing = scored[1:]
    print(f"\n>>> CANONICAL: property_id={canonical['property_id']} (score={canonical['score']:.4f})")
    winning_factor = max(canonical["subscores"], key=lambda k: _WEIGHTS[k] * canonical["subscores"][k])
    print(f"    Решающий фактор (наибольший вклад веса×суббалла): {winning_factor}")

    print(f"\n>>> {len(losing)} properties были бы помечены 'merged', их property_listings repointed:")
    for l in losing:
        pid = l["property_id"]
        listing_ids = [x["listing_id"] for x in facts[pid]["listings"]]
        print(f"  property_id={pid} (score={l['score']:.4f}) -> {len(listing_ids)} listing(s): {listing_ids}")

    print("\n>>> Конфликты атрибутов между canonical и losing properties:")
    c = facts[canonical["property_id"]]
    any_conflict = False
    for l in losing:
        f = facts[l["property_id"]]
        diffs = []
        if c["floor"] != f["floor"]:
            diffs.append(f"floor {c['floor']} vs {f['floor']}")
        if c["rooms"] != f["rooms"]:
            diffs.append(f"rooms {c['rooms']} vs {f['rooms']}")
        if c["complex_id"] != f["complex_id"]:
            diffs.append(f"complex_id {c['complex_id']} vs {f['complex_id']}")
        if c["area_sqm"] is not None and f["area_sqm"] is not None and abs(c["area_sqm"] - f["area_sqm"]) > 1.0:
            diffs.append(f"area {c['area_sqm']} vs {f['area_sqm']}")
        if diffs:
            any_conflict = True
            print(f"  canonical={canonical['property_id']} vs losing={f['property_id']}: {'; '.join(diffs)}")
    if not any_conflict:
        print("  ни одного конфликта floor/rooms/complex_id/area>1м² не найдено — согласованная группа.")

    print("\n>>> Rollback shape (append-only, если бы merge был выполнен):")
    print("  moved_listing_ids snapshot на каждую losing property (см. выше per-property список) "
          "-> при откате repoint обратно ТОЛЬКО этих listing_id, не всех текущих листингов canonical "
          "(см. docs/property_merge_design.md §5).")

    print("\n=== Полная таблица canonical-выбора по ВСЕМ компонентам (кратко) ===")
    for key, members in sorted(components.items(), key=lambda kv: -len(kv[1])):
        sc = _score_component(members, facts)
        if not sc:
            continue
        top = sc[0]
        print(f"  size={len(members):2d}  canonical={top['property_id']:>6}  score={top['score']:.3f}  "
              f"members={sorted(members)}")


if __name__ == "__main__":
    asyncio.run(main())
