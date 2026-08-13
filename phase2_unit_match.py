#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Фаза 2 (юниты), гейт п.Б — матчинг newbuild_units (застройщик) <->
apartment_listings (Крыша, is_new_build=TRUE). Задача 2026-08-13, см.
docs/entity_resolution_plan.md — numbers-first проход на реальных 28 ЖК
пересечения нашёл: этаж+метраж даёт 75 кандидатов, 40% (30) без единого
подтверждающего сигнала (цена/дата). Решение:

1. Номер квартиры (phase2_unit_number_coverage.py) — 100% на стороне
   застройщика (5 источников, каждый свой формат поля), 4% на стороне
   Крыши (2/50 — структурная нехватка данных, не алгоритм: описание
   объявления либо содержит "№N", либо нет). Есть на обеих сторонах И
   равны -> auto; есть и НЕ равны -> reject (перебивает остальные
   сигналы, это прямое противоречие, не просто "сигнала не хватило").
2. Иначе: этаж точное + метраж ±3м² + (цена ±5% ИЛИ пересечение дат
   активности) -> auto.
3. **Зеркальный кап**: если внутри блока (complex_id, комнатность) на
   ЛЮБОЙ стороне (застройщик ИЛИ Крыша) есть 2+ юнита с тем же (этаж,
   метраж±допуск) — планировка повторяется, пара неоднозначна даже при
   подтверждении цена/дата (Дом мог продать оба одинаковых юнита по
   похожей цене в похожее время) — только review, никогда auto.
4. Остальное — review (`unit_duplicate_candidates`).

Гейт: --limit ограничивает число РЕАЛЬНЫХ auto-разрешений за прогон
(review-записи пишутся всегда — дёшевы, обратимы, не часть гейта).

Запуск:
    venv/bin/python phase2_unit_match.py --limit 15         # гейт
    venv/bin/python phase2_unit_match.py --limit 1000        # массово
    venv/bin/python phase2_unit_match.py --limit 0 --dry     # только отчёт
"""
import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")
from dotenv import load_dotenv
load_dotenv()
import os

from phase2_unit_number_coverage import unit_number_newbuild, unit_number_listing

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

AREA_TOLERANCE_M2 = 3.0
PRICE_TOLERANCE_PCT = 0.05


def _haversine_unused():
    pass  # (нет гео-сигнала на юнитах — не нужен, оставлено пусто намеренно)


async def build_blocks(fetch):
    """(complex_id, rooms) -> {'nb': [...], 'al': [...]} — только для ЖК
    пересечения (numbers-first проход), не вся база. Найден живой баг
    гейта: прямой JOIN newbuild_units->apartment_listings (по имени ЖК)
    в одном запросе даёт fan-out — каждая nb-строка повторяется по разу
    на КАЖДУЮ al-строку того же ЖК (при 2+ al на комплекс — дубли nb),
    раздувая счётчики (1710->4281 блокированных пар, 75->204
    "кандидатов" на самом деле одни и те же пары посчитаны N раз).
    Фикс: сначала список ЖК пересечения ОТДЕЛЬНО (DISTINCT), потом
    nb/al запросы каждый сам по себе, без взаимного JOIN."""
    overlap_complex_ids = [r["complex_id"] for r in await fetch("""
        SELECT DISTINCT nu.complex_id
        FROM newbuild_units nu
        JOIN complexes c ON c.id = nu.complex_id
        JOIN apartment_listings al ON lower(trim(al.complex_name)) = lower(trim(c.name)) AND al.is_new_build = TRUE
    """)]
    if not overlap_complex_ids:
        return {}

    nb_rows = await fetch("""
        SELECT id, complex_id, rooms, floor, area, price,
               first_seen_at, last_seen_at, source, raw_json::text AS raw_json, source_unit_id
        FROM newbuild_units WHERE complex_id = ANY($1)
    """, overlap_complex_ids)
    al_rows = await fetch("""
        SELECT al.id, c.id AS complex_id, al.rooms, al.floor, al.area, al.price,
               al.first_seen, al.last_seen, al.description, al.title
        FROM apartment_listings al
        JOIN complexes c ON lower(trim(al.complex_name)) = lower(trim(c.name))
        WHERE c.id = ANY($1) AND al.is_new_build = TRUE
    """, overlap_complex_ids)
    blocks: dict[tuple[int, int], dict[str, list]] = {}
    for r in nb_rows:
        key = (r["complex_id"], r["rooms"])
        blocks.setdefault(key, {"nb": [], "al": []})["nb"].append(dict(r))
    for r in al_rows:
        key = (r["complex_id"], r["rooms"])
        blocks.setdefault(key, {"nb": [], "al": []})["al"].append(dict(r))
    return blocks


def _mirror_count(members: list[dict], floor, area) -> int:
    """Сколько юнитов В ТОМ ЖЕ списке (та же сторона, тот же блок)
    делят (этаж, метраж±допуск) — >1 значит планировка повторяется,
    сама пара неоднозначна независимо от подтверждения цена/дата."""
    if floor is None or area is None:
        return 0
    return sum(1 for m in members if m.get("floor") == floor and m.get("area") is not None
               and abs(m["area"] - area) <= AREA_TOLERANCE_M2)


def decide_pair(nb: dict, al: dict, nb_list: list[dict], al_list: list[dict]) -> tuple[str, str | None, dict]:
    """Решение по паре (nb, al) — вынесено из main() для тестируемости
    (tests/test_phase2_unit_match.py, регресс на реальных парах гейта
    2026-08-13). Возвращает (decision, reason, evidence):
    decision — 'auto' | 'review' | 'skip' ('skip' — прямое противоречие
    номера, не кандидат вовсе, не в очередь); reason — None для
    auto/skip, 'ambiguous_floorplan'|'no_confirmation' для review."""
    nb_num = unit_number_newbuild(nb["source"], nb["raw_json"], nb.get("source_unit_id"))
    al_num = unit_number_listing(al.get("description"), al.get("title"))

    evidence = {
        "floor_nb": nb["floor"], "floor_al": al["floor"],
        "area_nb": nb["area"], "area_al": al["area"],
        "price_nb": nb["price"], "price_al": al["price"],
        "unit_number_nb": nb_num, "unit_number_al": al_num,
    }

    if nb_num is not None and al_num is not None:
        if nb_num == al_num:
            evidence["method"] = "unit_number"
            return "auto", None, evidence
        return "skip", None, evidence  # прямое противоречие

    floor_ok = nb["floor"] is not None and nb["floor"] == al["floor"]
    area_ok = (nb["area"] is not None and al["area"] is not None
               and abs(nb["area"] - al["area"]) <= AREA_TOLERANCE_M2)
    if not (floor_ok and area_ok):
        return "skip", None, evidence  # даже не блокируется

    price_ok = bool(nb["price"] and al["price"]
                     and abs(nb["price"] - al["price"]) / al["price"] <= PRICE_TOLERANCE_PCT)
    date_ok = bool(nb.get("first_seen_at") and nb.get("last_seen_at") and al.get("first_seen") and al.get("last_seen")
                    and nb["first_seen_at"] <= al["last_seen"] and nb["last_seen_at"] >= al["first_seen"])
    evidence["price_ok"] = price_ok
    evidence["date_ok"] = date_ok

    mirror_nb = _mirror_count(nb_list, nb["floor"], nb["area"])
    mirror_al = _mirror_count(al_list, al["floor"], al["area"])
    evidence["mirror_count_nb"] = mirror_nb
    evidence["mirror_count_al"] = mirror_al

    if mirror_nb > 1 or mirror_al > 1:
        return "review", "ambiguous_floorplan", evidence
    if price_ok or date_ok:
        evidence["method"] = "floor_area_price" if price_ok else "floor_area_dates"
        return "auto", None, evidence
    return "review", "no_confirmation", evidence


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=15, help="макс. число РЕАЛЬНЫХ auto-разрешений за прогон (гейт)")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    from bot.db.pg import init_pool, close_pool, fetch, fetchval, execute

    await init_pool(DATABASE_URL)

    blocks = await build_blocks(fetch)
    print(f"блоков (complex_id, комнатность): {len(blocks)}")

    n_auto, n_reject, n_review = 0, 0, 0
    auto_done = 0
    stopped_early = False

    for (complex_id, rooms), members in sorted(blocks.items()):
        nb_list, al_list = members["nb"], members["al"]
        if not nb_list or not al_list:
            continue
        for nb in nb_list:
            for al in al_list:
                if auto_done >= args.limit and args.limit > 0:
                    stopped_early = True
                    break

                decision, reason, evidence = decide_pair(nb, al, nb_list, al_list)
                if decision == "skip":
                    if evidence.get("unit_number_nb") is not None and evidence.get("unit_number_al") is not None:
                        n_reject += 1  # прямое противоречие номера
                    continue

                if decision == "auto":
                    print(f"[AUTO] complex={complex_id} rooms={rooms} nb#{nb['id']} <-> al#{al['id']}: {evidence}")
                    if not args.dry:
                        now_iso = datetime.now(timezone.utc).isoformat()
                        conf = 1.0 if evidence.get("method") == "unit_number" else 0.85
                        await execute("""
                            INSERT INTO unit_source_links (unit_id, source, source_id, match_method, confidence, evidence, matched_by)
                            VALUES ($1, 'krisha', $2, $3, $4, $5, 'phase2_gate_2026-08-13')
                            ON CONFLICT (source, source_id) DO NOTHING
                        """, nb["id"], al["id"], evidence.get("method"), conf, json.dumps(evidence))
                        exists = await fetchval(
                            "SELECT 1 FROM unit_duplicate_candidates WHERE unit_id=$1 AND listing_id=$2", nb["id"], al["id"])
                        if exists:
                            await execute(
                                "DELETE FROM unit_duplicate_candidates WHERE unit_id=$1 AND listing_id=$2", nb["id"], al["id"])
                    n_auto += 1
                    auto_done += 1
                else:
                    n_review += 1
                    if not args.dry:
                        exists = await fetchval(
                            "SELECT 1 FROM unit_duplicate_candidates WHERE unit_id=$1 AND listing_id=$2", nb["id"], al["id"])
                        if not exists:
                            await execute("""
                                INSERT INTO unit_duplicate_candidates (unit_id, listing_id, complex_id, reason, evidence)
                                VALUES ($1, $2, $3, $4, $5)
                            """, nb["id"], al["id"], complex_id, reason, json.dumps(evidence))
            if stopped_early:
                break
        if stopped_early:
            break

    print(f"\n{'[DRY] ' if args.dry else ''}ИТОГ: auto={n_auto}, reject(число-противоречие)={n_reject}, "
          f"review={n_review}, остановлено гейтом (--limit {args.limit})={stopped_early}")

    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
