#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Авто-детектор кандидатов на расшивку — многоуровневый (задача
2026-08-13, второй проход: "многоуровневый подход", см.
docs/entity_resolution_plan.md — "расшивка как review-очередь").

Источники сигнала (каждый — отдельный "gather_*", возвращает свой кусок
evidence или None, если сигнала нет):

  1. `gather_krisha_deadlines` — `complexes.source_info->krisha.
     deadlines_by_queue` (парсится krisha_complex_import.py). 2+ записи
     с разными очередями и разными сроками — слабый, но дешёвый сигнал
     (уже лежит в БД, ничего фетчить не надо).
  2. `gather_homeportal_blocks` — ЧЕРЕЗ БД, не HTTP: `complex_source_
     links` (source='homeportal') JOIN `homeportal_objects` уже
     содержит per-объектный адрес/гео/`apartment_data[].spot_number`
     ("Очередь N"/"Блок N") — тот же источник, что unravel_blobs.py, но
     тут читается как ДОПОЛНИТЕЛЬНОЕ evidence для ЭТОЙ очереди, не для
     отдельного auto-split пайплайна homeportal.
  3. `gather_korter_blocks` / `gather_homsters_queues` — живая проверка
     2026-08-13 (см. docs): страницы обоих сайтов отдают ОДНУ запись
     на весь ЖК (schema.org JSON-LD `Apartment`/`Product` — один
     `yearBuilt`, один `geo`, один `address`), НИКАКОГО пер-блочного
     списка адресов/сроков структурно нет ни у одного из двух (Korter:
     проверено на ArmanTau Comfort 2 — "очередь" встретилась только в
     характеристике лифта, не как отдельная сущность; Homsters:
     проверено на MOD Urban — ни одного упоминания "очередь"/"корпус"/
     "срок сдачи" в JSON-LD ИЛИ рендер-данных страницы). Обе функции
     — задокументированные заглушки (всегда None), не пытаемся
     парсить то, чего нет — попытка выдумать структуру там, где сайт
     её не даёт, хуже отсутствия сигнала.
  4. `gather_apartment_listings_evidence` — исходная логика первого
     прохода (гео-кластеризация apartment_listings ВНУТРИ complex_id +
     явный маркер в title/description) — единственный источник,
     который может дать `reason='explicit_token_address'` (самый
     уверенный, откалиброван на живом прогоне 2026-08-13). Все
     остальные источники (1-3 выше) — только `multi_source_evidence`,
     решение заказчика: даже явный токен из homeportal/krisha-очередей
     тут не поднимает уверенность до "explicit_token_address" — та
     reason зарезервирована за уже откалиброванным путём.

Порядок работы (main): по всем complex_id без предыдущих split_
candidates → собрать evidence со ВСЕХ источников (дёшево — ни один
gather_* кроме apartment_listings-кластеризации не делает тяжёлых
запросов, а её и раньше делали) → decide_candidate() → записать.

Гейт авто-исполнения (`split_auto_execution_allowed` в
entity_resolution.py) — per-reason: 'explicit_token_address' может
когда-нибудь пройти гейт (10 решений, точность >=95%), у
'multi_source_evidence' решений пока 0 и накопление начинается с этого
прохода — обе reason проверяются тем же гейтом, но de facto только
explicit_token_address имеет шанс набрать точность близко к 95% на
имеющемся дизайне (multi_source_evidence по конструкции — сборная
солянка разных по силе сигналов, ожидаемо шумнее).

Запуск: venv/bin/python split_detect.py --test [--limit N]
"""
import argparse
import asyncio
import json
import sys
from collections import Counter

sys.path.insert(0, ".")
from dotenv import load_dotenv
load_dotenv()
import os

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

MIN_LISTINGS_PER_COMPLEX = 4
MIN_LISTINGS_PER_CLUSTER = 2
CLUSTER_DISTANCE_M = 1000.0  # тот же порог, что granularity policy правило 4 ("гео > 1 км")


class UnionFind:
    """Тот же класс, что unravel_blobs.py — не дублируем логику,
    другой источник объектов (listing id вместо object_id)."""
    def __init__(self, items):
        self.parent = {i: i for i in items}

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def _explicit_marker_token(text: str | None) -> str | None:
    """Только ЯВНО промаркированный токен (очередь/позиция/блок/
    корпус/литера) — те же анкорные regex, что _phase_token в
    entity_resolution.py, БЕЗ хвостовых фоллбэков (trailing-номер/
    trailing-буква) — те калиброваны под КОРОТКИЕ имена ЖК, на
    свободном тексте title/description дали бы шум."""
    from bot.core.entity_resolution import (
        _PHASE_QUEUE_BEFORE_RE, _PHASE_QUEUE_NUM_RE, _PHASE_QUEUE_AFTER_RE,
        _BLOCK_LETTER_BEFORE_RE, _BLOCK_LETTER_AFTER_RE,
        _PYATNO_KVARTAL_RE, _ENUM_LIST_RE,
    )
    if not text:
        return None
    s = text.lower()
    if _PYATNO_KVARTAL_RE.search(s) or _ENUM_LIST_RE.search(s):
        return None
    for pat in (_PHASE_QUEUE_BEFORE_RE, _PHASE_QUEUE_NUM_RE, _PHASE_QUEUE_AFTER_RE):
        m = pat.search(s)
        if m:
            return str(int(m.group(1)))
    for pat in (_BLOCK_LETTER_BEFORE_RE, _BLOCK_LETTER_AFTER_RE):
        m = pat.search(s)
        if m:
            return f"block:{m.group(1).lower()}"
    return None


def _display_token(token: str) -> str:
    return token.split(":", 1)[1].upper() if token.startswith("block:") else token


# ── 1. Крыша: срок сдачи по очередям (уже в БД, ничего не фетчим) ──────
def gather_krisha_deadlines(complex_row: dict) -> list[dict] | None:
    si = complex_row.get("source_info")
    if isinstance(si, str):
        si = json.loads(si) if si else {}
    dbq = ((si or {}).get("krisha") or {}).get("deadlines_by_queue")
    return dbq if dbq and len(dbq) >= 2 else None


# ── 2. Homeportal: через БД (complex_source_links + homeportal_objects), не HTTP ──
async def gather_homeportal_blocks(complex_id: int, fetch) -> tuple[list[dict] | None, list[str]]:
    """(blocks, tokens). blocks — None, если <2 объектов (нечего
    сравнивать) или набор объектов не даёт различимых токенов/адресов."""
    from bot.core.entity_resolution import _phase_token
    rows = await fetch("""
        SELECT ho.object_id, ho.name, ho.address, ho.latitude, ho.longitude, ho.apartment_data
        FROM complex_source_links csl
        JOIN homeportal_objects ho ON ho.object_id::text = csl.source_id
        WHERE csl.complex_id = $1 AND csl.source = 'homeportal'
    """, complex_id)
    if len(rows) < 2:
        return None, []
    blocks = []
    tokens = []
    for r in rows:
        token, _ = _phase_token(r["name"] or "")
        spot = None
        ad = r["apartment_data"]
        try:
            ad = json.loads(ad) if isinstance(ad, str) and ad else ad
            if ad:
                spot = ad[0].get("spot_number")
        except (ValueError, TypeError, IndexError, AttributeError):
            pass
        blocks.append({
            "object_id": r["object_id"], "name": r["name"], "address": r["address"],
            "lat": float(r["latitude"]) if r["latitude"] else None,
            "lon": float(r["longitude"]) if r["longitude"] else None,
            "spot_number": spot, "token": token,
        })
        if token:
            tokens.append(token)
    distinct_tokens = set(tokens)
    if len(distinct_tokens) < 2:
        return None, list(distinct_tokens)  # все объекты — одна и та же очередь/без токена вовсе
    return blocks, list(distinct_tokens)


# ── 3. Korter / Homsters — живая проверка 2026-08-13: НЕ отдают
# пер-блочные данные структурно (см. docstring модуля). Заглушки, не
# удаляем совсем — если сайты изменят структуру, есть куда дописать
# реальный парсинг, но выдумывать сейчас нечего.
def gather_korter_blocks(complex_id: int) -> None:
    return None


def gather_homsters_queues(complex_id: int) -> None:
    return None


# ── 4. apartment_listings (Крыша) — исходная логика первого прохода ────
def cluster_listings(listings: list[dict]) -> list[list[dict]]:
    from bot.core.entity_resolution import _haversine_m
    ids = [l["id"] for l in listings]
    uf = UnionFind(ids)
    for i in range(len(listings)):
        for j in range(i + 1, len(listings)):
            a, b = listings[i], listings[j]
            if _haversine_m(a["lat"], a["lon"], b["lat"], b["lon"]) <= CLUSTER_DISTANCE_M:
                uf.union(a["id"], b["id"])
    groups: dict[str, list[dict]] = {}
    by_id = {l["id"]: l for l in listings}
    for i in ids:
        groups.setdefault(uf.find(i), []).append(by_id[i])
    return sorted(groups.values(), key=len, reverse=True)


def gather_apartment_listings_evidence(parent_name: str, listings: list[dict]) -> dict | None:
    """{"clusters": [...], "has_explicit_token": bool} или None, если
    геокластеризовать нечего (нет 2+ кластеров с >=MIN_LISTINGS_PER_CLUSTER)."""
    if len(listings) < MIN_LISTINGS_PER_COMPLEX:
        return None
    clusters = cluster_listings(listings)
    big = [c for c in clusters if len(c) >= MIN_LISTINGS_PER_CLUSTER]
    if len(big) < 2:
        return None
    big.sort(key=len, reverse=True)
    passport, rest = big[0], big[1:]

    def _summarize(cl: list[dict], with_name: bool) -> dict:
        lat = sum(x["lat"] for x in cl) / len(cl)
        lon = sum(x["lon"] for x in cl) / len(cl)
        addr = next((x["address"] for x in cl if x.get("address")), None)
        tokens = Counter()
        for x in cl:
            t = _explicit_marker_token(x.get("title")) or _explicit_marker_token(x.get("description"))
            if t:
                tokens[t] += 1
        top_token = tokens.most_common(1)[0][0] if tokens else None
        d = {
            "n": len(cl), "lat": lat, "lon": lon, "address": addr,
            "tokens": list(tokens.keys()),
            "sample_listing_ids": [x["id"] for x in cl[:3]],
        }
        if with_name and top_token:
            d["suggested_name"] = f"{parent_name} {_display_token(top_token)}"
        return d

    cluster_summaries = [_summarize(passport, with_name=False)]
    all_have_tokens = True
    for cl in rest:
        s = _summarize(cl, with_name=True)
        if "suggested_name" not in s:
            all_have_tokens = False
        cluster_summaries.append(s)
    return {"clusters": cluster_summaries, "has_explicit_token": all_have_tokens}


# ── Комбинатор: решение reason + итоговый evidence ──────────────────────
def decide_candidate(al_evidence: dict | None, krisha_deadlines: list | None,
                      homeportal_blocks: list | None, homeportal_tokens: list[str],
                      korter_blocks: None, homsters_queues: None,
                      parent_name: str) -> tuple[str, dict] | None:
    explicit_tokens: list[str] = list(homeportal_tokens)
    if al_evidence:
        for cl in al_evidence["clusters"]:
            explicit_tokens.extend(cl.get("tokens", []))
    explicit_tokens = sorted(set(explicit_tokens))

    evidence = {
        "parent_name": parent_name,
        "krisha_deadlines": krisha_deadlines,
        "korter_blocks": korter_blocks,
        "homsters_queues": homsters_queues,
        "homeportal_blocks": homeportal_blocks,
        "apartment_listings_geo_clusters": al_evidence["clusters"] if al_evidence else None,
        "explicit_tokens": explicit_tokens,
    }

    # Самый уверенный путь — БЕЗ ИЗМЕНЕНИЙ с первого прохода: только
    # apartment_listings-геокластеры С явным маркером на каждом
    # не-паспортном кластере. Решение заказчика: остальные источники
    # (даже с явным токеном — homeportal/krisha) НЕ поднимают до этой
    # reason, только подтверждают multi_source_evidence.
    if al_evidence and al_evidence["has_explicit_token"]:
        return "explicit_token_address", evidence

    if krisha_deadlines or homeportal_blocks or al_evidence:
        return "multi_source_evidence", evidence

    return None


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="только показать, без записи")
    ap.add_argument("--limit", type=int, default=15, help="макс. новых кандидатов за прогон (гейт)")
    args = ap.parse_args()

    from bot.db.pg import init_pool, close_pool, fetch, execute
    from bot.core.entity_resolution import split_auto_execution_allowed, _execute_split_cluster

    await init_pool(DATABASE_URL)

    complexes = await fetch("""
        SELECT c.id, c.name, c.source_info FROM complexes c
        WHERE COALESCE(c.is_garbage, FALSE) = FALSE AND COALESCE(c.is_street, FALSE) = FALSE
          AND NOT EXISTS (SELECT 1 FROM split_candidates sc WHERE sc.complex_id = c.id)
    """)
    print(f"ЖК к проверке (без предыдущих split_candidates): {len(complexes)}")

    n_found = 0
    n_written = 0
    n_krisha_deadline_signal = 0
    n_homeportal_signal = 0
    for c in complexes:
        krisha_deadlines = gather_krisha_deadlines(dict(c))
        if krisha_deadlines:
            n_krisha_deadline_signal += 1
        homeportal_blocks, homeportal_tokens = await gather_homeportal_blocks(c["id"], fetch)
        if homeportal_blocks:
            n_homeportal_signal += 1
        korter_blocks = gather_korter_blocks(c["id"])
        homsters_queues = gather_homsters_queues(c["id"])

        listings = await fetch("""
            SELECT id, address, title, description, lat, lon
            FROM apartment_listings
            WHERE lower(trim(complex_name)) = lower(trim($1)) AND lat IS NOT NULL AND lon IS NOT NULL
        """, c["name"])
        al_evidence = gather_apartment_listings_evidence(c["name"], [dict(l) for l in listings])

        result = decide_candidate(al_evidence, krisha_deadlines, homeportal_blocks, homeportal_tokens,
                                   korter_blocks, homsters_queues, c["name"])
        if not result:
            continue
        reason, evidence = result
        n_found += 1
        sources = [k for k in ("krisha_deadlines", "homeportal_blocks", "apartment_listings_geo_clusters")
                   if evidence.get(k)]
        print(f"[CANDIDATE] complex={c['id']} '{c['name']}' reason={reason} sources={sources}")
        if args.test:
            continue
        if n_written >= args.limit:
            print(f"(гейт --limit={args.limit} достигнут, остальное — в следующий прогон)")
            break

        auto_ok = reason == "explicit_token_address" and await split_auto_execution_allowed(reason)
        if auto_ok:
            for cl in evidence["apartment_listings_geo_clusters"] or []:
                if cl.get("suggested_name"):
                    r = await _execute_split_cluster(c["id"], cl["suggested_name"], cl, "split_detect_auto")
                    print(f"  [AUTO-EXECUTED] {r}")
            await execute("""
                INSERT INTO split_candidates (complex_id, reason, evidence, matched_by, status, resolved_at, resolved_by)
                VALUES ($1, $2, $3::jsonb, 'split_detect_2026-08-13', 'approved', now(), 'split_detect_auto')
            """, c["id"], reason, json.dumps(evidence))
        else:
            await execute("""
                INSERT INTO split_candidates (complex_id, reason, evidence, matched_by)
                VALUES ($1, $2, $3::jsonb, 'split_detect_2026-08-13')
            """, c["id"], reason, json.dumps(evidence))
        n_written += 1

    print(f"\nИТОГ: найдено={n_found}, записано={n_written} "
          f"(сигнал krisha_deadlines={n_krisha_deadline_signal}, homeportal_blocks={n_homeportal_signal}) "
          f"({'test, ничего не записано' if args.test else 'live'})")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
