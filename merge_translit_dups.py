#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Массовый разбор транслит-дублей (задача гейта 2, следующий шаг после
Tandau, см. sweep_translit_dups.py и docs/entity_resolution_plan.md).

Критерий auto-мерджа (зафиксирован заказчиком): транслит-имена равны
(группировка — тот же ключ, что sweep_translit_dups.py,
transliterate(norm_name(name))) И хотя бы один подтверждающий сигнал —
реальное гео <=150 м с обеих сторон, ИЛИ тот же застройщик, ИЛИ
address_match() выше порога (используем существующий bool address_match()
— порог 50% overlap уже встроен в саму функцию). Продуктовый токен
(Highvill-пенальти, _product_token) на любой стороне пары — не auto,
всегда в review, даже если остальные сигналы совпали.

Выживает — у кого больше данных: complex_source_links + apartment_listings
+ enrichment (tech_specs/materials/reviews/фото/описание/конструктив).
Provenance: JSONB `complexes.provenance` на выжившей строке получает
{"merged_from": [...], "method": "translit_sweep_2026-08-12", "matched_by": ...}
(накопительно — не затирает уже бывшее там, напр. split_from от unravel_blobs.py).
Дубль помечается is_garbage=TRUE, свой provenance получает
{"merged_into": <canon_id>, ...} для трассировки.

Пары без подтверждающего сигнала или с продуктовым токеном — НЕ мерджатся,
уходят в complex_duplicate_candidates (kind='review', migrations/047).

Гейт: --limit N ограничивает число РЕАЛЬНЫХ мерджей за один запуск
(review-записи для полностью review-групп пишутся всегда, они дёшевы и
обратимы — не часть гейта). Скрипт идемпотентен и перезапускаемый —
уже смерженные (is_garbage) строки просто не попадают в группы заново,
уже записанные review-пары не дублируются (UNIQUE).

Запуск:
    venv/bin/python merge_translit_dups.py --limit 10          # гейт
    venv/bin/python merge_translit_dups.py --limit 1000         # массово
    venv/bin/python merge_translit_dups.py --limit 0 --dry      # только отчёт, без записи
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

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")


def _haversine_m(lat1, lon1, lat2, lon2):
    import math
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


class UnionFind:
    def __init__(self, items):
        self.parent = {x: x for x in items}

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


async def data_score(cid: int, fetchval) -> int:
    """links*2 + listings + enrichment (тех.специфика/материалы/отзывы/
    фото/описание/конструктив) — тот же дух, что merge_family_nest_dups.py/
    merge_tandau_dups.py, только шире (те смотрели только links+listings)."""
    name = await fetchval("SELECT name FROM complexes WHERE id = $1", cid)
    links = await fetchval("SELECT count(*) FROM complex_source_links WHERE complex_id = $1", cid)
    listings = await fetchval(
        "SELECT count(*) FROM apartment_listings WHERE lower(trim(complex_name)) = lower(trim($1))", name)
    tech = await fetchval("SELECT count(*) FROM complex_tech_specs WHERE complex_id = $1", cid)
    materials = await fetchval("SELECT count(*) FROM complex_materials WHERE complex_id = $1", cid)
    reviews = await fetchval("SELECT count(*) FROM complex_reviews WHERE complex_id = $1", cid)
    constructive = await fetchval("""
        SELECT (SELECT count(*) FROM complex_windows WHERE complex_id=$1)
             + (SELECT count(*) FROM complex_doors WHERE complex_id=$1)
             + (SELECT count(*) FROM complex_walls WHERE complex_id=$1)
             + (SELECT count(*) FROM complex_concrete_rebar WHERE complex_id=$1)
    """, cid)
    has_desc_photo = await fetchval(
        "SELECT (description IS NOT NULL)::int + (photo_url IS NOT NULL)::int FROM complexes WHERE id = $1", cid)
    return int(links) * 2 + int(listings) + int(tech) + int(materials) + int(reviews) + int(constructive) + int(has_desc_photo)


async def build_split_lineage(fetch) -> dict[int, int]:
    """id -> непосредственный split_from-родитель (только для строк, у
    которых он есть). Используется для проверки клана целиком (см.
    split_provenance_conflict) — цепочка может быть многоходовой: живой
    баг гейта #2 нашёл #311 -> #4266 -> #4339 (два хопа, не один), прямое
    сравнение "a.split_from == b.id" такое не ловит."""
    rows = await fetch("SELECT id, (provenance->>'split_from')::int AS parent "
                        "FROM complexes WHERE provenance ? 'split_from'")
    return {r["id"]: r["parent"] for r in rows if r["parent"] is not None}


def unravel_involved_ids(parent_of: dict[int, int]) -> set[int]:
    """ВСЕ id, тронутые сегодняшней расшивкой unravel_blobs.py — и дети
    (ключи parent_of), и паспорта-родители (значения). Более грубый и
    более надёжный предохранитель, чем split_provenance_conflict/
    phase_conflict по отдельности — найден ЧЕТВЁРТЫМ живым багом того же
    гейта: паспорт #1866 после расшивки на 3 очереди (2/1/3) НЕ
    переименован (`complexes.name` осталась голой "ЖК Altyn Saulet"),
    хотя реально он теперь представляет ИМЕННО "3 очередь" (только эти
    объекты остались в его spine-связях) — phase_conflict() смотрит
    только на имя, видит "голую" implicit-1 базу и мирно пропускает
    мердж с реальной '(очередь 1)' стороной, хотя по факту #1866 —
    3-я очередь, не 1-я. Пока `complexes.name` не синхронизировано с
    содержимым после расшивки (см. follow-up в docs/entity_resolution_
    plan.md), безопаснее вообще не давать транслит-мерджу трогать
    ничего из сегодняшней расшивки — только вручную, отдельным
    прогоном, после ручной сверки/переименования паспортов."""
    return set(parent_of.keys()) | set(parent_of.values())


def _split_ancestors(cid: int, parent_of: dict[int, int]) -> set[int]:
    chain = {cid}
    cur = cid
    while cur in parent_of and parent_of[cur] not in chain:
        cur = parent_of[cur]
        chain.add(cur)
    return chain


def split_provenance_conflict(a_id: int, b_id: int, parent_of: dict[int, int]) -> bool:
    """Более надёжный (и более дешёвый) предохранитель, чем phase_conflict()
    в одиночку — найден ВТОРЫМ живым случаем той же дыры (#311/#4339
    'Времена Года': '(Лето)' между базой и суффиксом блока сбивает
    base_sim < 0.8, phase_conflict() молчит, а сама связь оказалась
    двухходовой — #4339 отпочковался от #4266, а #4266 от #311, не
    напрямую). Если ветви родословной (сам id + все transitive
    split_from-предки) двух сторон пересекаются — это точно "один и тот
    же спорный blob, который намеренно разделили", блокируем БЕЗ разбора
    имени вообще, признак прямой, а не эвристика по тексту."""
    return bool(_split_ancestors(a_id, parent_of) & _split_ancestors(b_id, parent_of))


async def build_groups(fetch, transliterate, norm_name):
    rows = await fetch("""
        SELECT id, name, lat, lon, developer_id, developer, address FROM complexes
        WHERE COALESCE(is_garbage, FALSE) = FALSE AND COALESCE(is_street, FALSE) = FALSE
    """)
    groups: dict[str, list[dict]] = {}
    for r in rows:
        key = transliterate(norm_name(r["name"]))
        if not key or len(key) < 3:
            continue
        groups.setdefault(key, []).append(dict(r))
    return {k: v for k, v in groups.items() if len({x["id"] for x in v}) > 1}


async def phase_conflict(name_a_full: str, name_b_full: str) -> bool:
    """Реюз логики score_match()/_phase_token(): explicit-both-different
    ИЛИ implicit-phase-1-vs-N — конфликт (не мерджим, разные
    очереди/блоки, см. Darmen-ловушку). Найдено ЖИВЫМ багом гейта
    (первый прогон --limit 10 схлопнул #4272 'Abai Joly (3 очередь)'
    обратно в #1400 'Abai Joly' — norm_name() стирает "(3 очередь)"
    ДО группировки по транслит-ключу, так что группа их даже не
    различала; продуктовый токен эту дыру не ловил, т.к. это фаза,
    не линейка продукта). name_a_full/name_b_full — СЫРЫЕ имена, та же
    причина, что в score_match: скобки/суффиксы фазы теряются после
    norm_name().

    Третий живой баг (та же сессия, 'Altyn Saulet'/'Ауен'): implicit-
    phase-1-ветка сравнивает "голую" базу с базой номерованной стороны
    через обычный name_similarity() — в score_match() обе стороны почти
    всегда один язык (наш комплекс + один источник), а ЗДЕСЬ, в
    транслит-мердже, "голая" и "номерованная" стороны САМИ МОГУТ быть в
    разных алфавитах ('Алтын Саулет' vs 'Altyn Saulet (2 очередь)') —
    без транслита base_sim ~0, implicit-конфликт молчал, union-find
    склеивал явно разные очереди через "голый" мост. Пробуем транслит
    базы дополнительно, тот же приём, что в score_match()."""
    from bot.core.entity_resolution import _phase_token, name_similarity, transliterate
    phase_a, base_a = _phase_token(name_a_full)
    phase_b, base_b = _phase_token(name_b_full)
    if phase_a is not None and phase_b is not None:
        return phase_a != phase_b
    if phase_a is not None or phase_b is not None:
        bare_base, numbered_token, numbered_base = (
            (base_a, phase_b, base_b) if phase_a is None else (base_b, phase_a, base_a))
        base_sim = await name_similarity(bare_base, numbered_base) if bare_base and numbered_base else 0.0
        if base_sim < 0.8 and bare_base and numbered_base:
            base_sim = max(base_sim, await name_similarity(transliterate(bare_base), transliterate(numbered_base)))
        if base_sim >= 0.8:
            is_letter = numbered_token.startswith("block:")
            return is_letter or numbered_token != "1"
    return False


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=10, help="макс. число РЕАЛЬНЫХ мерджей за прогон (гейт)")
    ap.add_argument("--dry", action="store_true", help="ничего не пишет (ни мерджи, ни review), только печатает")
    args = ap.parse_args()

    from bot.db.pg import init_pool, close_pool, fetch, fetchval, execute
    from bot.core.entity_resolution import transliterate, address_match, _product_token
    from hype_tracker.homeportal_scan import norm_name

    await init_pool(DATABASE_URL)

    groups = await build_groups(fetch, transliterate, norm_name)
    parent_of = await build_split_lineage(fetch)
    involved = unravel_involved_ids(parent_of)
    print(f"транслит-групп (>1 complex_id, среди не is_garbage): {len(groups)}")
    print(f"id, тронутых сегодняшней расшивкой (исключены из auto-мерджа): {len(involved)}")

    n_merge_clusters, n_merged_ids, n_review_pairs = 0, 0, 0
    merges_done = 0
    stopped_early = False

    for key in sorted(groups):
        if merges_done >= args.limit and args.limit > 0:
            stopped_early = True
            break
        members = groups[key]
        ids = [m["id"] for m in members]
        by_id = {m["id"]: m for m in members}
        uf = UnionFind(ids)
        edge_evidence: dict[tuple[int, int], dict] = {}
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = members[i], members[j]
                pair = tuple(sorted((a["id"], b["id"])))
                product_a, product_b = _product_token(a["name"]), _product_token(b["name"])
                geo_ok = (a["lat"] is not None and a["lon"] is not None
                          and b["lat"] is not None and b["lon"] is not None
                          and _haversine_m(a["lat"], a["lon"], b["lat"], b["lon"]) <= 150)
                dev_ok = (a["developer_id"] is not None and a["developer_id"] == b["developer_id"]) or (
                    a["developer"] and b["developer"] and a["developer"].strip().lower() == b["developer"].strip().lower())
                addr_ok = address_match(a["address"], b["address"]) is True
                phase_bad = await phase_conflict(a["name"], b["name"])
                split_bad = (split_provenance_conflict(a["id"], b["id"], parent_of)
                             or a["id"] in involved or b["id"] in involved)
                evidence = {"geo_m": round(_haversine_m(a["lat"], a["lon"], b["lat"], b["lon"]), 1)
                            if (a["lat"] and a["lon"] and b["lat"] and b["lon"]) else None,
                            "same_developer": bool(dev_ok), "address_match": bool(addr_ok),
                            "product_a": product_a, "product_b": product_b,
                            "phase_conflict": bool(phase_bad), "split_provenance_conflict": bool(split_bad)}
                edge_evidence[pair] = evidence
                if (not phase_bad and not split_bad and product_a is None and product_b is None
                        and (geo_ok or dev_ok or addr_ok)):
                    uf.union(a["id"], b["id"])

        components: dict[int, list[int]] = {}
        for cid in ids:
            components.setdefault(uf.find(cid), []).append(cid)

        for root, comp_ids in components.items():
            if len(comp_ids) < 2:
                continue
            if merges_done >= args.limit and args.limit > 0:
                stopped_early = True
                break
            scores = {cid: await data_score(cid, fetchval) for cid in comp_ids}
            canon_id = max(scores, key=scores.get)
            dup_ids = [cid for cid in comp_ids if cid != canon_id]
            names = {cid: by_id[cid]["name"] for cid in comp_ids}
            print(f"\n[MERGE] {key!r}: канон #{canon_id} {names[canon_id]!r} (score={scores[canon_id]}) <- "
                  + ", ".join(f"#{d} {names[d]!r} (score={scores[d]})" for d in dup_ids))
            if not args.dry:
                canon_name = names[canon_id]
                for dup_id in dup_ids:
                    dup_name = names[dup_id]
                    n_listings = await fetchval(
                        "SELECT count(*) FROM apartment_listings WHERE lower(trim(complex_name)) = lower(trim($1))", dup_name)
                    await execute(
                        "UPDATE apartment_listings SET complex_name = $2 WHERE lower(trim(complex_name)) = lower(trim($1))",
                        dup_name, canon_name)
                    links = await fetch("SELECT source, source_id FROM complex_source_links WHERE complex_id = $1", dup_id)
                    moved = 0
                    for l in links:
                        exists = await fetchval(
                            "SELECT 1 FROM complex_source_links WHERE source=$1 AND source_id=$2 AND complex_id != $3",
                            l["source"], l["source_id"], dup_id)
                        if exists:
                            print(f"    ! source_link {l['source']}/{l['source_id']} уже есть у другого complex_id — не трогаю")
                            continue
                        await execute("UPDATE complex_source_links SET complex_id = $2 WHERE source=$1 AND source_id=$3",
                                      l["source"], canon_id, l["source_id"])
                        moved += 1
                    now_iso = datetime.now(timezone.utc).isoformat()
                    await execute("""
                        UPDATE complexes SET is_garbage = TRUE,
                            provenance = COALESCE(provenance, '{}'::jsonb) || $2::jsonb
                        WHERE id = $1
                    """, dup_id, json.dumps({"merged_into": canon_id, "method": "translit_sweep_2026-08-12",
                                              "matched_by": "auto", "merged_at": now_iso}))
                    await execute("""
                        UPDATE complexes SET
                            provenance = COALESCE(provenance, '{}'::jsonb) ||
                                jsonb_build_object('merged_from',
                                    COALESCE(provenance->'merged_from', '[]'::jsonb) || to_jsonb($2::int))
                        WHERE id = $1
                    """, canon_id, dup_id)
                    print(f"    #{dup_id} -> #{canon_id}: объявлений перенесено={n_listings}, links перенесено={moved}")
            n_merge_clusters += 1
            n_merged_ids += len(dup_ids)
            merges_done += 1

        # review — пары, которые НЕ оказались в одном компоненте
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = members[i], members[j]
                if uf.find(a["id"]) == uf.find(b["id"]):
                    continue
                pair = tuple(sorted((a["id"], b["id"])))
                ev = edge_evidence[pair]
                if ev.get("split_provenance_conflict"):
                    reason = "split_provenance_conflict"
                elif ev.get("phase_conflict"):
                    reason = "phase_conflict"
                elif ev["product_a"] or ev["product_b"]:
                    reason = "product_token_mismatch"
                else:
                    reason = "no_confirming_signal"
                if not args.dry:
                    exists = await fetchval(
                        "SELECT 1 FROM complex_duplicate_candidates WHERE complex_id_a=$1 AND complex_id_b=$2",
                        pair[0], pair[1])
                    if not exists:
                        await execute("""
                            INSERT INTO complex_duplicate_candidates
                                (complex_id_a, complex_id_b, translit_key, reason, evidence)
                            VALUES ($1, $2, $3, $4, $5)
                        """, pair[0], pair[1], key, reason, json.dumps(ev))
                n_review_pairs += 1

    print(f"\n{'[DRY] ' if args.dry else ''}ИТОГ: merge-кластеров={n_merge_clusters} "
          f"(id смёржено в канон={n_merged_ids}), review-пар={n_review_pairs}, "
          f"остановлено гейтом (--limit {args.limit})={stopped_early}")

    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
