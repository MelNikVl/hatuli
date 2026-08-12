#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gap-driven sweep (задача 2026-08-12, п.3 — "главный приоритет"): для
~867 ЖК без единой связи с источником, но С координатами (см.
/admin/entity-ids, разбивка "нерезолвнутых"), целимся в каталоги
Крыши и Korter, а не ждём, пока их случайно принесёт другой парсер.

Крыша не даёт текстового поиска по имени (`/complex/search/astana/
?query=...` — проверено вручную, параметр query молча игнорируется,
это server-rendered ПАГИНИРОВАННЫЙ каталог, не поисковая выдача) —
поэтому подход двухступенчатый:

  Stage A (дёшево, ~105 запросов): один раз проходим весь пагинированный
    каталог Астаны (`?page=N`, 12 карточек/стр., ~1249 ЖК всего) —
    берём id/alias/имя/адрес прямо из карточки (data-атрибуты), без
    захода на детальные страницы.
  Stage B (бесплатно, локально): для каждого из наших нерезолвленных ЖК
    ищем кандидата в этом каталоге тем же вейтлифтом, что и в
    hype_tracker/homeportal_scan.py (точное/нормализованное/fuzzy имя).
  Stage C (прицельно, только для реально найденных кандидатов — не для
    всех 867 и не для всего каталога): заходим на детальную страницу
    НАЙДЕННОГО кандидата (`krisha_complex_import.parse_complex_page` —
    уже проверенный код, вытаскивает гео/застройщика/адрес), считаем
    score_match() с реальными сигналами (это и есть "по координатам" —
    гео с детальной страницы источника против гео нашего ЖК), пишем
    record_source_link().

Korter: полноценного текстового поиска тоже нет, зато уже есть daily
краулер всего каталога (korter_import.fetch_all(), 9 страниц
класс/район) — переиспользуем его вместо повторного изобретения; имя +
застройщик (адреса/гео Korter-карточки не дают), так что auto оттуда
маловероятен (нет геосигнала), но кандидаты корректно уходят в очередь.

Запуск:
    venv/bin/python gap_sweep_krisha_korter.py --test --limit-pages 5 --limit 20
    venv/bin/python gap_sweep_krisha_korter.py                # полный прогон
"""
from __future__ import annotations

import argparse
import asyncio
import random
import re
import sys

sys.path.insert(0, ".")
from dotenv import load_dotenv
load_dotenv()
import os

import httpx
from bs4 import BeautifulSoup

from hype_tracker.homeportal_scan import norm_name
from krisha_complex_import import parse_complex_page, BASE as KRISHA_BASE, HEADERS as KRISHA_HEADERS

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

KRISHA_SEARCH_URL = f"{KRISHA_BASE}/complex/search/astana/"
KRISHA_LIST_DELAY = (1.0, 2.0)      # Stage A: лёгкие страницы каталога
KRISHA_DETAIL_DELAY = (3.0, 6.0)    # Stage C: детальные страницы (щадяще, как krisha_complex_import.py)
FUZZY_THRESHOLD = 0.75              # тот же порог, что у homeportal_scan.py


# ── Stage A: весь каталог Крыши по Астане, постранично ──────────────────

async def fetch_krisha_catalog(client: httpx.AsyncClient, max_pages: int = 110) -> list[dict]:
    catalog: list[dict] = []
    seen_ids: set[str] = set()
    page = 1
    while page <= max_pages:
        try:
            resp = await client.get(KRISHA_SEARCH_URL, params={"page": page})
        except Exception as e:
            print(f"  стр {page}: ошибка запроса {e}, стоп")
            break
        if resp.status_code != 200:
            print(f"  стр {page}: HTTP {resp.status_code}, стоп")
            break
        soup = BeautifulSoup(resp.text, "html.parser")
        cards = soup.select("div.complex-card[data-complex-id]")
        if not cards:
            print(f"  стр {page}: карточек не найдено, конец каталога")
            break
        new_on_page = 0
        for card in cards:
            cid = card.get("data-complex-id")
            if not cid or cid in seen_ids:
                continue
            seen_ids.add(cid)
            alias = card.get("data-complex-alias") or ""
            name = card.get("data-name") or ""
            addr_el = card.select_one(".complex-card__address")
            address = addr_el.get_text(strip=True) if addr_el else None
            if name and alias:
                catalog.append({"krisha_id": cid, "alias": alias, "name": name, "address": address})
                new_on_page += 1
        if new_on_page == 0:
            print(f"  стр {page}: только уже виденные — конец каталога")
            break
        page += 1
        await asyncio.sleep(random.uniform(*KRISHA_LIST_DELAY))
    print(f"Stage A: каталог Крыши — {len(catalog)} ЖК с {page - 1} страниц")
    return catalog


# ── Stage B: локальный вейтлифт (без сети) — тот же паттерн, что у
#    homeportal_scan.py: точное -> нормализованное -> fuzzy.

def build_indexes(catalog: list[dict]) -> tuple[dict, dict]:
    exact_idx = {c["name"].strip().lower(): c for c in catalog}
    norm_idx: dict[str, dict] = {}
    for c in catalog:
        nm = norm_name(c["name"])
        if nm and nm not in norm_idx:
            norm_idx[nm] = c
    return exact_idx, norm_idx


def find_candidate(name: str, exact_idx: dict, norm_idx: dict) -> tuple[dict | None, str | None]:
    import difflib
    c = exact_idx.get(name.strip().lower())
    if c:
        return c, "exact"
    nm = norm_name(name)
    if not nm:
        return None, None
    c = norm_idx.get(nm)
    if c:
        return c, "normalized"
    best_ratio, best_c = 0.0, None
    for cand_norm, cand in norm_idx.items():
        ratio = difflib.SequenceMatcher(None, nm, cand_norm).ratio()
        if ratio > best_ratio:
            best_ratio, best_c = ratio, cand
    if best_ratio >= FUZZY_THRESHOLD:
        return best_c, f"fuzzy:{best_ratio:.2f}"
    return None, None


async def run_krisha(client: httpx.AsyncClient, unresolved: list[dict], catalog: list[dict],
                      limit: int | None, dry: bool) -> dict:
    from bot.db.pg import fetchrow
    from bot.core.entity_resolution import score_match, record_source_link, name_similarity, FUZZY_NAME_THRESHOLD

    exact_idx, norm_idx = build_indexes(catalog)
    stats: dict[str, int] = {"no_candidate": 0, "pretrim": 0}
    n = 0
    for row in unresolved:
        if limit and n >= limit:
            break
        cand, find_tier = find_candidate(row["name"], exact_idx, norm_idx)
        if not cand:
            stats["no_candidate"] += 1
            continue
        # Дешёвая предфильтрация ПЕРЕД сетевым запросом: локальный вейтлифт
        # (difflib) и реальный скоринг (pg_trgm) расходятся на коротких/
        # мусорных именах чаще, чем хотелось бы ("aru park" -> "Auez Park"
        # по difflib 0.82, по pg_trgm ~0) — раз score_match() всё равно
        # даст no_match ниже FUZZY_NAME_THRESHOLD, детальную страницу за
        # этим не тянем вовсе, экономим запрос.
        pre_sim = await name_similarity(row["name"], cand["name"])
        if pre_sim < FUZZY_NAME_THRESHOLD:
            stats["pretrim"] += 1
            continue
        n += 1
        # Stage C: детальная страница НАЙДЕННОГО кандидата — гео/застройщик/
        # адрес, "по координатам" сигнал, а не общая карточка списка.
        url = f"{KRISHA_BASE}/complex/show/{cand['alias']}/"
        try:
            resp = await client.get(url)
            detail = parse_complex_page(resp.text, url) if resp.status_code == 200 else {}
        except Exception as e:
            print(f"  {row['name']!r}: детальная стр. не открылась ({e}), считаем без гео")
            detail = {}
        # Застройщик у нас — developers.name (через developer_id), у Крыши
        # с детальной страницы — тоже имя (не БИН, тот там не публикуется)
        # — сравниваем имена, не БИН с именем (та ошибка была в первой
        # версии скрипта, поймано до боевого прогона).
        dev_name_existing = None
        if row.get("developer_id"):
            dr = await fetchrow("SELECT name FROM developers WHERE id = $1", row["developer_id"])
            dev_name_existing = dr["name"] if dr else None
        developer_match = bool(dev_name_existing and detail.get("developer")) and (
            dev_name_existing.strip().lower() in detail["developer"].strip().lower()
            or detail["developer"].strip().lower() in dev_name_existing.strip().lower())
        conf, method = await score_match(
            row["name"], cand["name"],
            existing_lat=row["lat"], existing_lon=row["lon"],
            candidate_lat=detail.get("lat"), candidate_lon=detail.get("lon"),
            developer_match=developer_match,
            existing_address=row.get("address"), candidate_address=detail.get("address") or cand.get("address"),
            name_a_full=row["name"], name_b_full=cand["name"],
        )
        if dry:
            print(f"  [dry] {row['name']!r:40} -> #{cand['krisha_id']} {cand['name']!r:35} "
                  f"({find_tier}) conf={conf:.2f} {method}")
            result = "dry"
        else:
            result = await record_source_link(
                row["id"], "krisha", cand["krisha_id"], url=url,
                confidence=conf, method=method, matched_by="auto")
            print(f"  {row['name']!r:40} -> #{cand['krisha_id']} {cand['name']!r:35} "
                  f"({find_tier}) conf={conf:.2f} {method:45} {result}")
        stats[result] = stats.get(result, 0) + 1
        await asyncio.sleep(random.uniform(*KRISHA_DETAIL_DELAY))
    return stats


# ── Korter: полноценного поиска тоже нет, переиспользуем daily-краулер
#    всего каталога (korter_import.fetch_all) — имя + застройщик, без
#    гео/адреса (Korter-карточка списка их не даёт), так что auto почти
#    никогда не наберётся (максимум 0.6+0.15=0.75) — кандидаты корректно
#    уходят в очередь на подтверждение, не в спайн молча.

async def run_korter(unresolved: list[dict], limit: int | None, dry: bool, test: bool) -> dict:
    from korter_import import fetch_all
    from bot.db.pg import fetchrow
    from bot.core.entity_resolution import score_match, record_source_link

    print("Korter: тяну daily-каталог (9 страниц класс/район)...")
    korter_found = await fetch_all(test=test)  # {norm_name: {name, url, developer, district, ...}}
    print(f"Korter: каталог — {len(korter_found)} ЖК")

    stats: dict[str, int] = {"no_candidate": 0}
    n = 0
    for row in unresolved:
        if limit and n >= limit:
            break
        nm = norm_name(row["name"])
        entry = korter_found.get(nm)
        find_tier = "normalized" if entry else None
        if not entry:
            import difflib
            best_ratio, best_entry = 0.0, None
            for cand_norm, cand in korter_found.items():
                ratio = difflib.SequenceMatcher(None, nm, cand_norm).ratio()
                if ratio > best_ratio:
                    best_ratio, best_entry = ratio, cand
            if best_ratio >= FUZZY_THRESHOLD:
                entry, find_tier = best_entry, f"fuzzy:{best_ratio:.2f}"
        if not entry:
            stats["no_candidate"] += 1
            continue
        n += 1
        dev_row = None
        if row.get("developer_id"):
            dev_row = await fetchrow("SELECT name FROM developers WHERE id=$1", row["developer_id"])
        developer_match = bool(dev_row and entry.get("developer")
                                and dev_row["name"].strip().lower() in entry["developer"].strip().lower())
        conf, method = await score_match(
            row["name"], entry["name"],
            developer_match=developer_match,
            name_a_full=row["name"], name_b_full=entry["name"],
        )
        source_id = entry.get("url") or nm
        if dry:
            print(f"  [dry] {row['name']!r:40} -> {entry['name']!r:35} ({find_tier}) conf={conf:.2f} {method}")
            result = "dry"
        else:
            result = await record_source_link(
                row["id"], "korter", source_id, url=entry.get("url"),
                confidence=conf, method=method, matched_by="auto")
            print(f"  {row['name']!r:40} -> {entry['name']!r:35} ({find_tier}) conf={conf:.2f} {method:45} {result}")
        stats[result] = stats.get(result, 0) + 1
    return stats


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="dry-run: ничего не пишет в БД, только печатает")
    ap.add_argument("--limit-pages", type=int, default=0, help="ограничить Stage A (страницы каталога Крыши)")
    ap.add_argument("--limit", type=int, default=0, help="ограничить число обрабатываемых ЖК (для проверки)")
    ap.add_argument("--skip-korter", action="store_true")
    ap.add_argument("--skip-krisha", action="store_true")
    args = ap.parse_args()

    from bot.db.pg import init_pool, close_pool, fetch
    await init_pool(DATABASE_URL)

    unresolved = await fetch("""
        SELECT c.id, c.name, c.lat, c.lon, c.address, c.developer_id
        FROM complexes c
        WHERE COALESCE(c.is_garbage, FALSE) = FALSE AND COALESCE(c.is_street, FALSE) = FALSE
          AND NOT EXISTS (SELECT 1 FROM complex_source_links l WHERE l.complex_id = c.id)
          AND c.lat IS NOT NULL AND c.lon IS NOT NULL
        ORDER BY c.id
    """)
    unresolved = [dict(r) for r in unresolved]
    print(f"нерезолвленных ЖК с гео: {len(unresolved)}")

    if not args.skip_krisha:
        print("\n=== Krisha ===")
        async with httpx.AsyncClient(headers=KRISHA_HEADERS, timeout=30.0, follow_redirects=True) as client:
            catalog = await fetch_krisha_catalog(client, max_pages=args.limit_pages or 110)
            krisha_stats = await run_krisha(client, unresolved, catalog, args.limit or None, args.test)
        print("Krisha итог:", krisha_stats)

    if not args.skip_korter:
        print("\n=== Korter ===")
        korter_stats = await run_korter(unresolved, args.limit or None, args.test, test=args.test)
        print("Korter итог:", korter_stats)

    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
