#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Разовый перескор review-очереди (complex_source_link_candidates)
текущим score_match() (задача гейта 2, п.А — очередь копилась под
СТАРЫМИ версиями скоринга, до транслита/Highvill-пенальти/address-
noise-фикса; пересчитываем актуальным кодом, не гадаем).

Правило: confidence >= AUTO_MATCH_THRESHOLD -> перенос в spine
(complex_source_links, provenance через match_method-суффикс, как
approve_candidate(), но matched_by='rescore_2026-08-13' — отличать от
руками подтверждённых); confidence < REVIEW_QUEUE_THRESHOLD -> снять
(DELETE, сигнал слишком слаб даже для очереди); середина — остаётся
человеку, но confidence/match_method обновляются на актуальные (не
тихо устаревшие).

krisha: candidate-имя нигде не кэшировано (Stage C гейта брало его с
живой карточки в момент матча) — ре-фетчим сохранённый url, title
страницы -> имя. korter: тоже без кэша, но дешевле — один прогон
korter_import.fetch_all() (daily-каталог, 9 страниц) вместо N
детальных запросов, ищем по url.

Запуск: venv/bin/python rescore_review_queue.py [--dry]
"""
import argparse
import asyncio
import json
import re
import sys

sys.path.insert(0, ".")
from dotenv import load_dotenv
load_dotenv()
import os

import httpx

from krisha_complex_import import parse_complex_page, HEADERS as KRISHA_HEADERS

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

_TITLE_RE = re.compile(r"<title>([^<]+)</title>")
_JSONLD_NAME_RE = re.compile(r'"name"\s*:\s*"([^"]+)"')


def _extract_krisha_name(html: str) -> str | None:
    """Приоритет — JSON-LD `"name": "ЖК Salt"` (первое вхождение —
    сам ЖК, а не карточки квартир/организации ниже по странице), самое
    чистое имя на странице. Найдено ЖИВЫМ багом: <title> добавляет
    город + маркетинговый хвост ("ЖК Alatau Eco Park АСТАНА: 🏘️ цены,
    планировки | BR Building - Крыша") — этого достаточно, чтобы sim
    упал ниже FUZZY_NAME_THRESHOLD и дал ложный no_match на РЕАЛЬНО
    совпадающих ЖК. <title> — только fallback, если JSON-LD почему-то
    нет."""
    m = _JSONLD_NAME_RE.search(html)
    if m:
        return m.group(1).strip() or None
    m = _TITLE_RE.search(html)
    if not m:
        return None
    title = re.split(r"[:|]", m.group(1))[0].strip()
    return title or None


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    from bot.db.pg import init_pool, close_pool, fetch, fetchrow, fetchval, execute
    from bot.core.entity_resolution import (
        score_match, AUTO_MATCH_THRESHOLD, REVIEW_QUEUE_THRESHOLD, record_source_link,
        name_similarity, address_match, _haversine_m)
    from hype_tracker.homeportal_scan import norm_name

    await init_pool(DATABASE_URL)

    rows = await fetch("SELECT * FROM complex_source_link_candidates WHERE kind = 'review' ORDER BY source, id")
    print(f"review-очередь: {len(rows)} записей")

    korter_catalog: dict[str, dict] = {}
    if any(r["source"] == "korter" for r in rows):
        from korter_import import fetch_all
        print("тяну korter daily-каталог (9 страниц)...")
        by_norm = await fetch_all(test=False)
        korter_catalog = {v["url"]: v for v in by_norm.values() if v.get("url")}
        print(f"korter: каталог — {len(korter_catalog)} ЖК")

    stats = {"auto": 0, "removed": 0, "still_review": 0, "fetch_error": 0}
    async with httpx.AsyncClient(headers=KRISHA_HEADERS, timeout=30.0, follow_redirects=True) as client:
        for r in rows:
            cx = await fetchrow("SELECT name, lat, lon, address, developer_id FROM complexes WHERE id = $1", r["complex_id"])
            if not cx:
                continue
            dev_name_existing = None
            if cx["developer_id"]:
                dr = await fetchrow("SELECT name FROM developers WHERE id = $1", cx["developer_id"])
                dev_name_existing = dr["name"] if dr else None

            cand_name = cand_address = None
            cand_lat = cand_lon = None
            developer_match = False

            if r["source"] == "krisha":
                try:
                    resp = await client.get(r["url"])
                    detail = parse_complex_page(resp.text, r["url"]) if resp.status_code == 200 else {}
                    cand_name = _extract_krisha_name(resp.text) if resp.status_code == 200 else None
                except Exception as e:
                    print(f"  #{r['id']} complex={r['complex_id']}: ошибка запроса ({e})")
                    stats["fetch_error"] += 1
                    continue
                cand_address = detail.get("address")
                cand_lat, cand_lon = detail.get("lat"), detail.get("lon")
                developer_match = bool(dev_name_existing and detail.get("developer")) and (
                    dev_name_existing.strip().lower() in detail["developer"].strip().lower()
                    or detail["developer"].strip().lower() in dev_name_existing.strip().lower())
            elif r["source"] == "korter":
                entry = korter_catalog.get(r["source_id"])
                if not entry:
                    print(f"  #{r['id']} complex={r['complex_id']}: url {r['source_id']} больше нет в каталоге korter — снимаю")
                    if not args.dry:
                        await execute("DELETE FROM complex_source_link_candidates WHERE id = $1", r["id"])
                    stats["removed"] += 1
                    continue
                cand_name = entry.get("name")
                developer_match = bool(dev_name_existing and entry.get("developer")
                                        and dev_name_existing.strip().lower() in entry["developer"].strip().lower())
            else:
                print(f"  #{r['id']}: неизвестный source={r['source']!r}, пропуск")
                continue

            if not cand_name:
                print(f"  #{r['id']} complex={r['complex_id']}: имя кандидата не извлеклось, оставляю как есть")
                stats["still_review"] += 1
                continue

            conf, method = await score_match(
                norm_name(cand_name), norm_name(cx["name"]),
                existing_lat=cx["lat"], existing_lon=cx["lon"],
                candidate_lat=cand_lat, candidate_lon=cand_lon,
                developer_match=developer_match,
                existing_address=cx["address"], candidate_address=cand_address,
                name_a_full=cand_name, name_b_full=cx["name"],
            )
            print(f"  #{r['id']} {r['source']} complex={r['complex_id']} {cx['name']!r}: "
                  f"{float(r['confidence']):.2f} -> {conf:.2f} ({method})")

            # evidence инлайн для review-UI (задача "очередь кандидатов",
            # 2026-08-13) — численные дельты, не только имена сигналов в
            # match_method.
            geo_m = round(_haversine_m(cx["lat"], cx["lon"], cand_lat, cand_lon), 1) \
                if (cx["lat"] is not None and cx["lon"] is not None
                    and cand_lat is not None and cand_lon is not None) else None
            evidence = {
                "name_sim": round(await name_similarity(norm_name(cand_name), norm_name(cx["name"])), 2),
                "geo_m": geo_m, "same_developer": bool(developer_match),
                "address_match": bool(address_match(cx["address"], cand_address)),
                "candidate_name": cand_name, "candidate_address": cand_address,
            }

            if conf >= AUTO_MATCH_THRESHOLD:
                stats["auto"] += 1
                if not args.dry:
                    result = await record_source_link(
                        r["complex_id"], r["source"], r["source_id"], url=r["url"],
                        confidence=conf, method=method, matched_by="rescore_2026-08-13", evidence=evidence)
                    # already_linked/conflict/rejected — record_source_link сам разрулил,
                    # старую review-строку убираем в любом случае (перескор её решил).
                    await execute("DELETE FROM complex_source_link_candidates WHERE id = $1", r["id"])
                    print(f"    -> {result}")
            elif conf < REVIEW_QUEUE_THRESHOLD:
                stats["removed"] += 1
                if not args.dry:
                    await execute("DELETE FROM complex_source_link_candidates WHERE id = $1", r["id"])
            else:
                stats["still_review"] += 1
                if not args.dry:
                    await execute(
                        "UPDATE complex_source_link_candidates SET confidence = $2, match_method = $3, evidence = $4 WHERE id = $1",
                        r["id"], conf, method, json.dumps(evidence))

    print(f"\n{'[DRY] ' if args.dry else ''}ИТОГ: {stats}")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
