#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Интеграционный перескор spine auto-связей (задача 2026-08-12, гейт 2 —
после карантина координат/unravel_blobs.py address_match() получил
дополнительную зачистку шума ("р."/"уч."/название района, см. коммит
"address_match(): район/уч. — тоже шум, не сигнал"). Нужно перескорить
все связи, которые реально прошли через score_match() (matched_by='auto'),
свежим кодом — placeholder-гео уже вырезан geo_quarantine*.py прямо в
колонках (latitude/longitude = NULL), так что "без плацебо-гео" не нужно
реализовывать отдельно: просто читаем текущие колонки.

Что НЕ перескориваем (не продукт score_match(), пересчитывать нечего):
  - match_method='legacy_import' (backfill из старых однослотовых колонок,
    confidence=1.0 захардкожен, никогда не считался score_match'ем);
  - match_method='seed_source'/'manual' (bi_group seed-связь, unravel-сплиты —
    тоже не score_match).

Источники с реальными matched_by='auto' связями сейчас:
  - krisha (gap_sweep_krisha_korter.py, Stage C): гео/застройщик/имя уже
    в момент матча зашиты в сохранённый confidence/method (можно разложить
    обратно по сигнатуре "name_X+geo+developer+phase(N)" — веса
    зафиксированы в entity_resolution.py), но candidate_address НИГДЕ не
    закэширован (страница ЖК не хранит адрес отдельной колонкой) —
    единственный способ перескорить именно address-сигнал без искажения
    остальных (геосигнал у реального дома не "дрейфует", ре-фетчить его
    ради этого не нужно) — сходить на ту же сохранённую url ещё раз и
    взять свежий адрес оттуда.
  - homeportal (hype_tracker/homeportal_scan.py): сейчас 0 живых auto-строк
    (все 585 в spine — legacy_import/unravel, см. отчёт), но код-путь
    оставлен рабочим на будущее — данные все локальные (homeportal_objects
    JOIN complexes), без сети.

Запуск: venv/bin/python rescore_spine_auto.py [--test]
"""
import argparse
import asyncio
import re
import sys

sys.path.insert(0, ".")
from dotenv import load_dotenv
load_dotenv()
import os

import httpx

from krisha_complex_import import parse_complex_page, HEADERS as KRISHA_HEADERS

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

# Только реальные score_match()-продукты — komponента "+address" не может
# появиться сама по себе ни в одной другой сигнатуре метода.
_SCORE_MATCH_METHOD_RE = re.compile(r"^(name_exact|name_fuzzy\()")


async def rescore_krisha(client: httpx.AsyncClient, rows: list[dict], dry: bool) -> dict:
    from bot.core.entity_resolution import address_match, AUTO_MATCH_THRESHOLD, _W_ADDRESS, record_source_link
    from bot.db.pg import fetchval, execute

    stats = {"total": len(rows), "fetch_error": 0, "address_now_matches": 0,
              "already_had_address": 0, "unchanged": 0, "demoted": 0}
    for r in rows:
        existing_address = await fetchval("SELECT address FROM complexes WHERE id = $1", r["complex_id"])
        if "+address" in r["match_method"]:
            stats["already_had_address"] += 1
            continue
        try:
            resp = await client.get(r["url"])
            detail = parse_complex_page(resp.text, r["url"]) if resp.status_code == 200 else {}
        except Exception as e:
            print(f"  #{r['id']} complex={r['complex_id']} {r['url']}: ошибка запроса ({e})")
            stats["fetch_error"] += 1
            continue
        candidate_address = detail.get("address")
        addr_ok = address_match(existing_address, candidate_address)
        if not addr_ok:
            stats["unchanged"] += 1
            continue
        stats["address_now_matches"] += 1
        new_conf = round(min(float(r["confidence"]) + _W_ADDRESS, 1.0), 2)
        new_method = r["match_method"] + "+address"
        print(f"  #{r['id']} complex={r['complex_id']}: address теперь совпадает "
              f"({existing_address!r} ~ {candidate_address!r}) "
              f"{r['confidence']:.2f} -> {new_conf:.2f} ({new_method})")
        if new_conf < AUTO_MATCH_THRESHOLD:
            # алгебраически недостижимо (добавление сигнала может только расти),
            # но защитный путь на случай будущих изменений весов — реальная
            # демоция в очередь review, не тихая правка на месте.
            stats["demoted"] += 1
            if not dry:
                await execute("DELETE FROM complex_source_links WHERE id = $1", r["id"])
                await record_source_link(r["complex_id"], r["source"], r["source_id"],
                                          confidence=new_conf, method=new_method, matched_by="auto")
            continue
        if not dry:
            await execute(
                "UPDATE complex_source_links SET confidence = $2, match_method = $3 WHERE id = $1",
                r["id"], new_conf, new_method)
    return stats


async def rescore_homeportal(rows: list[dict], dry: bool) -> dict:
    """Код-путь для будущих matched_by='auto' записей homeportal — полностью
    локальный (без сети), lat/lon уже прошли карантин placeholder-значений
    прямо в колонках homeportal_objects/complexes (geo_quarantine*.py их
    занулил), так что здесь достаточно читать текущее состояние."""
    from bot.core.entity_resolution import score_match, AUTO_MATCH_THRESHOLD, record_source_link
    from hype_tracker.homeportal_scan import norm_name
    from bot.db.pg import fetchrow, fetchval, execute

    stats = {"total": len(rows), "unchanged": 0, "demoted": 0, "error": 0}
    for r in rows:
        hp = await fetchrow("SELECT name, address, latitude, longitude, developer_bin FROM homeportal_objects WHERE object_id = $1", int(r["source_id"]))
        cx = await fetchrow("SELECT name, lat, lon, address FROM complexes WHERE id = $1", r["complex_id"])
        if not hp or not cx:
            stats["error"] += 1
            continue
        dev_bin = await fetchval("SELECT developer_bin FROM complex_tech_specs WHERE complex_id = $1", r["complex_id"])
        conf, method = await score_match(
            norm_name(hp["name"]), norm_name(cx["name"]),
            existing_lat=cx["lat"], existing_lon=cx["lon"],
            candidate_lat=float(hp["latitude"]) if hp["latitude"] else None,
            candidate_lon=float(hp["longitude"]) if hp["longitude"] else None,
            developer_match=bool(dev_bin) and dev_bin == hp["developer_bin"],
            existing_address=cx["address"], candidate_address=hp["address"],
            name_a_full=hp["name"], name_b_full=cx["name"],
        )
        print(f"  #{r['id']} complex={r['complex_id']} object={r['source_id']}: "
              f"{r['confidence']:.2f} -> {conf:.2f} ({method})")
        if conf < AUTO_MATCH_THRESHOLD:
            stats["demoted"] += 1
            if not dry:
                await execute("DELETE FROM complex_source_links WHERE id = $1", r["id"])
                await record_source_link(r["complex_id"], r["source"], r["source_id"],
                                          confidence=conf, method=method, matched_by="auto")
        else:
            stats["unchanged"] += 1
            if not dry and (conf != float(r["confidence"]) or method != r["match_method"]):
                await execute(
                    "UPDATE complex_source_links SET confidence = $2, match_method = $3 WHERE id = $1",
                    r["id"], conf, method)
    return stats


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="ничего не пишет в БД")
    args = ap.parse_args()

    from bot.db.pg import init_pool, close_pool, fetch
    await init_pool(DATABASE_URL)

    all_auto = await fetch("SELECT id, complex_id, source, source_id, url, match_method, confidence "
                            "FROM complex_source_links WHERE matched_by = 'auto' ORDER BY source, id")
    all_auto = [dict(r) for r in all_auto]
    scoreable = [r for r in all_auto if _SCORE_MATCH_METHOD_RE.match(r["match_method"])]
    not_scoreable = [r for r in all_auto if r not in scoreable]

    print(f"matched_by='auto' всего: {len(all_auto)}")
    print(f"  из них — продукт score_match() (перескориваем): {len(scoreable)}")
    for r in not_scoreable:
        print(f"  пропуск (не score_match, method={r['match_method']!r}): "
              f"#{r['id']} source={r['source']} complex={r['complex_id']}")

    by_source: dict[str, list[dict]] = {}
    for r in scoreable:
        by_source.setdefault(r["source"], []).append(r)

    report: dict[str, dict] = {}
    if by_source.get("krisha"):
        print(f"\n=== krisha ({len(by_source['krisha'])}) ===")
        async with httpx.AsyncClient(headers=KRISHA_HEADERS, timeout=30.0, follow_redirects=True) as client:
            report["krisha"] = await rescore_krisha(client, by_source["krisha"], args.test)
        print("krisha итог:", report["krisha"])

    if by_source.get("homeportal"):
        print(f"\n=== homeportal ({len(by_source['homeportal'])}) ===")
        report["homeportal"] = await rescore_homeportal(by_source["homeportal"], args.test)
        print("homeportal итог:", report["homeportal"])

    for src in set(by_source) - {"krisha", "homeportal"}:
        print(f"\n=== {src} ({len(by_source[src])}) — нет реализованного пути перескора, "
              f"тот же паттерн что krisha/homeportal не написан (сигналов пока не было) ===")

    print(f"\n{'[TEST] ' if args.test else ''}ИТОГ: "
          f"auto-связей всего={len(all_auto)}, "
          f"score_match-продукт={len(scoreable)}, "
          f"не score_match (пропущено)={len(not_scoreable)}, "
          f"по источникам={report}")

    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
