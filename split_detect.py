#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Авто-детектор кандидатов на расшивку (задача 2026-08-13, см.
docs/entity_resolution_plan.md — "расшивка как review-очередь"),
зеркало unravel_blobs.py, но источник сигнала другой:

  unravel_blobs.py: homeportal_objects — адрес ОБЪЕКТА против ЖК,
    явный токен в ИМЕНИ homeportal-объекта.
  split_detect.py:  apartment_listings (Крыша) ВНУТРИ одного
    complex_id — гео-кластеризация объявлений (>1км друг от друга,
    тот же порог, что granularity policy правило 4) + явный маркер
    очереди/блока/литера В TITLE/DESCRIPTION конкретного объявления
    (не в имени ЖК — там для этого уже есть _phase_token/
    unravel_blobs.py на homeportal-стороне).

Почему НЕ парсим "Расположение"/"Срок сдачи" со страницы ЖК Крыши
(krisha_complex_import.py) как источник кластеров: живая проверка
2026-08-13 (Rio de Janeiro) показала — сайт отдаёт ОДНУ строку адреса
на весь ЖК ("ул. Бейбарыс Султан, 23, 25, 25/2, 25/3, 25/4, 27") без
привязки конкретного дома к конкретной очереди, и ОДНУ строку срока
сдачи с очередями через точку с запятой — распарсить это в надёжное
"очередь N → дом M" сопоставление нельзя (порядок домов в адресе не
гарантированно совпадает с порядком очередей в сроке сдачи). Эта
строка срока сдачи всё равно парсится в krisha_complex_import.py как
обогащение (source_info->krisha.deadlines_by_queue) — не как сигнал
для этого детектора.

Правила (см. docs/entity_resolution_plan.md, "политика гранулярности
ЖК" — те же правила 1-4, только сторона данных другая):
  - 2+ гео-кластера, У КАЖДОГО не-паспортного — явный маркер
    (очередь/позиция/блок/корпус/литера) хотя бы на одном объявлении
    -> reason='explicit_token_address'.
  - 2+ гео-кластера, хотя бы у одного не-паспортного маркера НЕТ
    -> reason='address_diverge_no_token', маркер "решает человек"
    (тот же случай, что AUSTRIA в granularity policy — адрес один не
    решает).
  - "Пятно N"/"квартал N"/перечисление номеров в тексте — НЕ токен
    (те же _PYATNO_KVARTAL_RE/_ENUM_LIST_RE, что _phase_token).

Авто-исполнение (создание/provenance-бэкафилл child-complex без
approve) — гейт: entity_resolution.split_auto_execution_allowed(reason)
must быть True (10 решённых вручную, точность >=95% на reason-корзине).
Сейчас всегда False (0 решений) — весь вывод идёт в review.

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
    свободном тексте title/description дали бы шум (см. docstring
    модуля)."""
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


def cluster_listings(listings: list[dict]) -> list[list[dict]]:
    """Гео-кластеризация union-find'ом, порог CLUSTER_DISTANCE_M (тот
    же принцип, что unravel_blobs.py::analyze_complex — там по
    address_match(), тут по факту гео, потому что сравниваем ОДНУ и ту
    же complex-строку объявлений, у которых обычно нет отдельного
    поля адреса такого же качества, как у homeportal)."""
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


def build_evidence(parent_name: str, clusters: list[list[dict]]) -> tuple[str, dict] | None:
    """(reason, evidence) или None, если после фильтра мелких кластеров
    расщеплять нечего. Паспорт — крупнейший кластер (тот же принцип,
    что complex_data_score/canon в transлит-мердже — "кто больше,
    тот и остаётся"), остальные — кандидаты на дочерние complex_id."""
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

    reason = "explicit_token_address" if all_have_tokens else "address_diverge_no_token"
    return reason, {"parent_name": parent_name, "clusters": cluster_summaries}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="только показать, без записи")
    ap.add_argument("--limit", type=int, default=15, help="макс. новых кандидатов за прогон (гейт)")
    args = ap.parse_args()

    from bot.db.pg import init_pool, close_pool, fetch, fetchval, execute
    from bot.core.entity_resolution import split_auto_execution_allowed, _execute_split_cluster

    await init_pool(DATABASE_URL)

    complexes = await fetch("""
        SELECT c.id, c.name FROM complexes c
        WHERE COALESCE(c.is_garbage, FALSE) = FALSE AND COALESCE(c.is_street, FALSE) = FALSE
          AND NOT EXISTS (SELECT 1 FROM split_candidates sc WHERE sc.complex_id = c.id)
    """)
    print(f"ЖК к проверке (без предыдущих split_candidates): {len(complexes)}")

    n_found = 0
    n_written = 0
    for c in complexes:
        listings = await fetch("""
            SELECT id, address, title, description, lat, lon
            FROM apartment_listings
            WHERE lower(trim(complex_name)) = lower(trim($1)) AND lat IS NOT NULL AND lon IS NOT NULL
        """, c["name"])
        if len(listings) < MIN_LISTINGS_PER_COMPLEX:
            continue
        clusters = cluster_listings([dict(l) for l in listings])
        result = build_evidence(c["name"], clusters)
        if not result:
            continue
        reason, evidence = result
        n_found += 1
        print(f"[CANDIDATE] complex={c['id']} '{c['name']}' reason={reason} "
              f"clusters={[cl['n'] for cl in evidence['clusters']]}")
        if args.test:
            continue
        if n_written >= args.limit:
            print(f"(гейт --limit={args.limit} достигнут, остальное — в следующий прогон)")
            break

        auto_ok = await split_auto_execution_allowed(reason) and reason == "explicit_token_address"
        if auto_ok:
            for cl in evidence["clusters"]:
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

    print(f"\nИТОГ: найдено={n_found}, записано={n_written} ({'test, ничего не записано' if args.test else 'live'})")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
