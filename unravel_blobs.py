#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Расшивка blob-комплексов (задача 2026-08-12, Gate 2, см.
docs/entity_resolution_plan.md — "политика гранулярности ЖК").

Алгоритм (правила 1-5 в docs):
  1. Доверенные (не закарантиненные) homeportal-объекты ЖК делятся на
     "без явного токена" (_phase_token() -> None) и "с явным токеном".
  2. Если токенов нет вовсе — расшивать нечего (нет сигнала); если среди
     безномерных всё же есть адресное расхождение >1 км — это отдельный
     кейс для review (правило 4), не авто-решение (сюда, в этот скрипт,
     не включаем — просто печатаем в отчёте "нет токенов, но есть
     доверенное расхождение", ничего не пишем).
  3. Токен-объекты кластеризуются между собой ПО АДРЕСУ (address_match(),
     union-find) — Parkland showed: разные явные токены (1,2,C,D) могут
     быть одним кластером, если у них один адрес (см. таблицу вердиктов).
  4. Паспорт (исходный complex_id) — безномерная группа, если есть;
     иначе крупнейший адресный кластер среди токен-объектов.
  5. Каждый ДРУГОЙ кластер -> новый complex_id: имя/гео/адрес/застройщик
     из кластера, provenance={"split_from", "split_at", "method"};
     homeportal source_links кластера переносятся на новый complex_id
     (match_method='manual', matched_by='unravel', evidence=...).
     apartment_listings НЕ переносятся автоматически (нет надёжного
     способа сопоставить конкретное объявление конкретному блоку без
     дополнительного ручного разбора) — остаются на паспорте.

Запуск: venv/bin/python unravel_blobs.py --test [--limit N]
"""
import argparse
import asyncio
import json
import re
import statistics
import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")
from dotenv import load_dotenv
load_dotenv()
import os

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

# ── Review-router (задача 2026-08-12, Gate 2 — ответ на находку AUSTRIA) ──
# auto-split рискованнее там, где кластер отличается ТОЛЬКО голым
# числовым блок/корпус-токеном (без букв, без "N-я очередь" — те несут
# больше уверенности сами по себе) И родительский ЖК — "мега" (много
# явно промаркированных блок-номеров суммарно). Порог — тюнящаяся
# константа, не физическая константа: 5 выбрано по единственному живому
# примеру (AUSTRIA, блоки 1..11 -> 11 маркированных номеров, явно мега;
# Времена Года — блок-маркеры только у {1,2,3,4} = 4, ниже порога,
# остаётся auto). Пересмотреть после массового прогона, если увидим
# ложные срабатывания в любую сторону.
ROUTER_MIN_PARENT_BLOCK_NUMBERS = 5

# Только явно МАРКИРОВАННЫЕ "блок/блоки/корпус N" номера — совпадает с
# тем, что вообще попадает в scope роутера (голый номер без маркера, если
# такой у объекта единственный сигнал, и так не пройдёт транспарентно
# нигде — но такие "родитель мега" не считаем: считаем то, что явно
# промаркировано как блок, это и есть сигнал "тут много блоков").
_MARKED_BLOCK_NUMS_RE = re.compile(r"(?:блок|блоки|корпус)[а-яё]*\s*[№#]?\s*([\d,\s]+)", re.I)


def _parent_marked_block_numbers(names: list[str]) -> set[str]:
    nums: set[str] = set()
    for name in names:
        for m in _MARKED_BLOCK_NUMS_RE.finditer(name):
            for piece in re.split(r"[,\s]+", m.group(1).strip()):
                if piece.isdigit():
                    nums.add(piece)
    return nums


def cluster_needs_review(cluster_names: list[str], parent_names: list[str]) -> bool:
    """True — предлагаемый split кластера уходит в review, не auto.

    Срабатывает, только когда ОБА условия верны:
      (а) кластер сам по себе — только голый числовой блок/корпус-токен на
          КАЖДОМ своём объекте (ни одной буквы, ни одной явной "N-я
          очередь"/"очередь N" фразы — те несут собственную уверенность,
          не нуждаются в этом предохранителе);
      (б) родительский ЖК (весь набор его homeportal-объектов, не только
          кластер) явно маркирует >= ROUTER_MIN_PARENT_BLOCK_NUMBERS
          разных блок-номеров суммарно — "мега"-признак (AUSTRIA: блоки
          1..11 промаркированы явно = 11; Времена Года: явно
          промаркированы только {1,2,3,4} = 4, "-5" без маркера "блок" не
          считается, а именная фаза "Лето" в кластере сама выводит его
          из-под правила (а) в примере из docs, тут для арифметики
          неважно)."""
    # Буква-блок (используем _phase_token, а не свою регулярку, чтобы не
    # разойтись с тем, что реально нашло кластеризацию) ИЛИ явная
    # "очередь" где-либо в имени объекта — выводит кластер из-под роутера.
    from bot.core.entity_resolution import _phase_token
    only_bare_numeric = True
    for n in cluster_names:
        token, _ = _phase_token(n)
        if token is not None and token.startswith("block:"):
            only_bare_numeric = False
            break
        if "очеред" in n.lower():
            only_bare_numeric = False
            break
    if not only_bare_numeric:
        return False
    parent_block_nums = _parent_marked_block_numbers(parent_names)
    return len(parent_block_nums) >= ROUTER_MIN_PARENT_BLOCK_NUMBERS


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


async def analyze_complex(cid, objs, fetchrow, address_match, phase_token):
    """Возвращает план: {passport: [...], new_clusters: [[...], ...], no_signal: bool}."""
    no_token = [o for o in objs if phase_token(o["name"])[0] is None]
    token_objs = [o for o in objs if phase_token(o["name"])[0] is not None]

    if not token_objs:
        return {"no_signal": True, "no_token": no_token, "token_objs": [], "clusters": []}

    uf = UnionFind([o["object_id"] for o in token_objs])
    for i in range(len(token_objs)):
        for j in range(i + 1, len(token_objs)):
            a, b = token_objs[i], token_objs[j]
            if a["address"] and b["address"] and address_match(a["address"], b["address"]):
                uf.union(a["object_id"], b["object_id"])

    groups: dict[int, list] = {}
    for o in token_objs:
        groups.setdefault(uf.find(o["object_id"]), []).append(o)
    clusters = list(groups.values())
    clusters.sort(key=len, reverse=True)

    return {"no_signal": False, "no_token": no_token, "token_objs": token_objs, "clusters": clusters}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="ограничить число комплексов (для Gate 2 — первые N)")
    args = ap.parse_args()

    from bot.db.pg import init_pool, close_pool, fetch, fetchrow, execute
    from bot.core.entity_resolution import address_match, _phase_token

    await init_pool(DATABASE_URL)

    # НЕ фильтруем geo_quarantined_at IS NULL здесь — карантин обнуляет
    # только гео (см. geo_quarantine.py/geo_quarantine_placeholders.py),
    # имя и адрес у закарантиненных объектов остаются рабочими и нужны
    # кластеризации (адрес — первичный сигнал, правило 5 в docs). Гео
    # просто будет NULL у таких строк — используется только как
    # подтверждение/сомнение, отсутствие гео не блокирует кластеризацию.
    rows = await fetch("""
        SELECT matched_complex_id, object_id, name, address, developer_name,
               latitude::float AS lat, longitude::float AS lon
        FROM homeportal_objects
        WHERE matched_complex_id IS NOT NULL
    """)
    by_complex: dict[int, list] = {}
    for r in rows:
        by_complex.setdefault(r["matched_complex_id"], []).append(dict(r))

    candidates = [cid for cid, objs in by_complex.items() if len(objs) >= 2]
    candidates.sort()
    if args.limit:
        candidates = candidates[:args.limit]
    print(f"комплексов к разбору: {len(candidates)}")

    n_auto, n_review, n_no_signal, n_single_cluster = 0, 0, 0, 0
    review_routed = []  # для недельного ритуала — печатаем списком в конце
    for cid in candidates:
        objs = by_complex[cid]
        plan = await analyze_complex(cid, objs, fetchrow, address_match, _phase_token)
        cx = await fetch("SELECT name, lat, lon, developer_id, address FROM complexes WHERE id = $1", cid)
        cx = cx[0] if cx else None
        cx_name = cx["name"] if cx else "?"

        if plan["no_signal"]:
            n_no_signal += 1
            continue

        clusters = plan["clusters"]
        no_token = plan["no_token"]
        # паспорт: безномерная группа, если есть, иначе крупнейший кластер
        if no_token:
            passport_ids = {o["object_id"] for o in no_token}
            split_clusters = clusters  # ВСЕ token-кластеры уходят в новые (правило 2)
        else:
            passport_cluster = clusters[0]
            passport_ids = {o["object_id"] for o in passport_cluster}
            split_clusters = clusters[1:]

        if not split_clusters:
            n_single_cluster += 1
            continue

        parent_names = [o["name"] for o in objs]
        auto_clusters, review_clusters = [], []
        for cl in split_clusters:
            names = [o["name"] for o in cl]
            if cluster_needs_review(names, parent_names):
                review_clusters.append(cl)
            else:
                auto_clusters.append(cl)

        if review_clusters:
            n_review += 1
            for cl in review_clusters:
                review_routed.append((cid, cx_name, [o["name"] for o in cl]))
            print(f"\n#{cid} {cx_name!r} — REVIEW ({len(review_clusters)} кластеров, "
                  f"мега-родитель, только голые номера блоков):")
            for cl in review_clusters:
                print(f"  ?? не трогаю: {[o['name'] for o in cl]}")

        if not auto_clusters:
            continue

        n_auto += 1
        print(f"\n#{cid} {cx_name!r} — SPLIT auto ({len(auto_clusters)} новых кластеров):")
        print(f"  паспорт остаётся #{cid}: {[o['name'] for o in objs if o['object_id'] in passport_ids]}")
        for cl in auto_clusters:
            names = [o["name"] for o in cl]
            addrs = {o["address"] for o in cl if o["address"]}
            lats = [o["lat"] for o in cl if o["lat"] is not None]
            lons = [o["lon"] for o in cl if o["lon"] is not None]
            new_lat = statistics.median(lats) if lats else None
            new_lon = statistics.median(lons) if lons else None
            print(f"  -> новый комплекс: {names}")
            print(f"     адрес(а): {addrs}")
            print(f"     гео: ({new_lat}, {new_lon})")

            if not args.test:
                seed_name = names[0]
                provenance = json.dumps({"split_from": cid, "split_at": datetime.now(timezone.utc).isoformat(),
                                          "method": "unravel_2026-08-12"})
                try:
                    new_cid = await fetchrow("""
                        INSERT INTO complexes (name, developer_id, address, lat, lon, provenance, updated_at)
                        VALUES ($1, $2, $3, $4, $5, $6, now())
                        RETURNING id
                    """, seed_name, cx["developer_id"] if cx else None,
                        next(iter(addrs), None), new_lat, new_lon, provenance)
                except Exception as e:
                    # complexes.name уникально (тоже совпадает в двух разных
                    # blob'ах, редко, но встретилось — "Panorama park (блок
                    # 3)") — не роняем весь массовый прогон, дизамбигуируем
                    # object_id первого объекта кластера, он точно уникален.
                    if "unique" not in str(e).lower() and "duplicate" not in str(e).lower():
                        raise
                    seed_name = f"{seed_name} [{cl[0]['object_id']}]"
                    print(f"     ! коллизия имени, дизамбигуация -> {seed_name!r}")
                    new_cid = await fetchrow("""
                        INSERT INTO complexes (name, developer_id, address, lat, lon, provenance, updated_at)
                        VALUES ($1, $2, $3, $4, $5, $6, now())
                        RETURNING id
                    """, seed_name, cx["developer_id"] if cx else None,
                        next(iter(addrs), None), new_lat, new_lon, provenance)
                new_cid = new_cid["id"]
                for o in cl:
                    await execute("""
                        UPDATE homeportal_objects SET matched_complex_id = $2 WHERE object_id = $1
                    """, o["object_id"], new_cid)
                    await execute("""
                        UPDATE complex_source_links SET
                            complex_id = $2, match_method = 'manual', matched_by = 'unravel',
                            evidence = $3
                        WHERE source = 'homeportal' AND source_id = $1
                    """, str(o["object_id"]), new_cid,
                        json.dumps({"unravel_from": cid, "cluster_addresses": list(addrs),
                                    "cluster_members": names}))
                print(f"     -> создан complex #{new_cid}")

    print(f"\nИТОГ: auto-split-комплексов={n_auto}, review-routed-комплексов={n_review}, "
          f"один кластер (не split)={n_single_cluster}, без сигнала={n_no_signal}, "
          f"всего рассмотрено={len(candidates)}")
    if review_routed:
        print("\nСписок review-routed (для недельного ритуала):")
        for cid, cx_name, names in review_routed:
            print(f"  #{cid} {cx_name!r}: {names}")

    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
