#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""audit_property_linker_fuzzy.py — READ-ONLY аудит качества fuzzy-веток
bot/identity/property_linker.py (задача 2026-08-16, "аудит property
linker fuzzy matching" — прямое продолжение STOP на preflight backfill'а:
dry-run разошёлся с ожиданием на >10%, главный подозреваемый — 11617
fuzzy matches, которые могли ошибочно склеить РАЗНЫЕ квартиры одного ЖК
на одном этаже похожей площади в один property_id).

НИЧЕГО НЕ ПИШЕТ: ни в properties/property_listings, ни в какую-либо
другую таблицу — только SELECT. НЕ меняет bot/identity/property_linker.py
("не менять production linker на этом этапе" — задача).

Архитектура: НЕ дёргает реальный async link_listing_to_property() 50283
раза x 13 прогонов (точки 3+5 задачи) — это были бы сотни тысяч round-
trip'ов к БД, непрактично. Вместо этого — ЧИСТАЯ, синхронная simulate_
linking() в памяти, зеркалящая ТЕ ЖЕ правила (exact-hash-хэш ->
fuzzy(complex+floor+area±tolerance, ближайший) -> новая квартира),
параметризованная RuleConfig для вариантов A-F (задача, п.5). Для
rule=RULES["A_baseline"] зеркалит текущий линковщик 1:1 — проверено
кросс-чеком со scripts/backfill_property_ids.py --dry-run (см. тесты и
финальный отчёт: totals должны совпасть на одинаковом входе/порядке).
EXACT-хэш-шаг НЕ варьируется ни у одного из A-F — не предмет этого
аудита (сомнения были именно в fuzzy-ветке, не в SHA1-точном совпадении).

Запуск:
    venv/bin/python scripts/audit_property_linker_fuzzy.py
    venv/bin/python scripts/audit_property_linker_fuzzy.py --samples-out audit_fuzzy_samples.log
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv()

# logging.basicConfig() — ДО импорта bot.identity.property_linker/
# seller_profile_snapshot: если что-то по цепочке их импортов уже
# сконфигурировало root logger раньше нас, basicConfig() ниже стал бы
# no-op (проверено эмпирически на первом реальном прогоне — файл лога
# оставался пустым 0 байт, хотя print() в stdout работал штатно).
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.FileHandler("audit_property_linker_fuzzy.log", encoding="utf-8", errors="replace")],
)
log = logging.getLogger("audit_property_linker_fuzzy")

# Единый источник правды для хэша/нормализации адреса/fuzzy-confidence —
# ИМПОРТ, не копия (иначе аудит мог бы незаметно разойтись с реальным
# линковщиком). tolerance читаем оттуда же (текущее значение прод-правила).
from bot.identity.property_linker import (
    normalize_address, compute_address_hash, _fuzzy_confidence, _FUZZY_AREA_TOLERANCE,
)
from seller_profile_snapshot import _normalize_name

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")


# ── Best-effort номер дома (диагностика ЭТОГО аудита, НЕ авторитетный
# источник — normalize_address() в property_linker.py его не извлекает
# отдельно вовсе, только схлопывает в общую строку хэша) ──────────────
_HOUSE_NUMBER_RE = re.compile(r"(\d+[а-яА-Яa-zA-Z]?(?:[/\-]\d+[а-яА-Яa-zA-Z]?)?)\s*,?\s*$")


def extract_house_number(address: str | None) -> str | None:
    """Последняя группа "число(+буква/дробь)" в конце СЫРОГО адреса
    ("Кабанбай батыра 15" -> "15", "Момышулы 10/2" -> "10/2"). None, если
    в адресе вообще нет числа в конце или адрес пуст — эвристика, не
    гадаем дальше этого."""
    if not address:
        return None
    m = _HOUSE_NUMBER_RE.search(address.strip())
    return m.group(1).lower() if m else None


# ── Правила fuzzy-принятия (задача, п.5 "A-F") ──────────────────────────

@dataclass(frozen=True)
class RuleConfig:
    name: str
    area_tolerance: float = _FUZZY_AREA_TOLERANCE
    require_rooms_match: bool = False
    require_house_number_match: bool = False
    single_candidate_only: bool = False


RULES: dict[str, RuleConfig] = {
    "A_baseline": RuleConfig("A_baseline"),
    "B_rooms": RuleConfig("B_rooms", require_rooms_match=True),
    "C_house_number": RuleConfig("C_house_number", require_house_number_match=True),
    "D_tolerance_0.5": RuleConfig("D_tolerance_0.5", area_tolerance=0.5),
    "E_single_candidate": RuleConfig("E_single_candidate", single_candidate_only=True),
    "F_combined": RuleConfig(
        "F_combined", area_tolerance=0.5, require_rooms_match=True,
        require_house_number_match=True, single_candidate_only=True),
}


# ── Загрузка данных ──────────────────────────────────────────────────────

async def _load_complex_lookup() -> dict[str, int]:
    """lower(trim(name)) -> complexes.id — тот же лукап, что property_
    linker.py::_resolve_complex_id, но ОДИН проход вместо async-запроса
    на каждую из 50к+ строк (аудиту нужна скорость симуляции, не только
    правильность). ORDER BY id ASC + "первый выигрывает" при дублирующихся
    именах — детерминированно, не зависит от порядка обработки listings
    (не вносит свой шум в order-sensitivity тесты, п.3 задачи)."""
    from bot.db.pg import fetch
    rows = await fetch("SELECT id, name FROM complexes WHERE name IS NOT NULL ORDER BY id ASC")
    lookup: dict[str, int] = {}
    for r in rows:
        key = r["name"].strip().lower()
        if key and key not in lookup:
            lookup[key] = r["id"]
    return lookup


async def _load_rows() -> list[dict]:
    """Один SELECT БЕЗ ORDER BY — "текущий" порядок п.3 задачи (что БД
    физически возвращает без явной сортировки). complex_id резолвится
    отдельно (_load_complex_lookup) и подмешивается здесь в Python, не
    через JOIN — JOIN на неуникальном lower(trim(name)) мог бы размножить
    строки при дублирующихся именах ЖК."""
    from bot.db.pg import fetch
    lookup = await _load_complex_lookup()
    rows = await fetch("""
        SELECT id, address, floor, area, rooms, complex_name,
               first_seen, last_seen, archived_at, is_active,
               seller_name, price, lat, lon
        FROM apartment_listings
    """)
    out = []
    for r in rows:
        d = dict(r)
        cn = d.get("complex_name")
        d["complex_id"] = lookup.get(cn.strip().lower()) if cn else None
        out.append(d)
    return out


# ── Порядки для теста устойчивости (задача, п.3) ────────────────────────

def order_variants(rows: list[dict]) -> dict[str, list[dict]]:
    """"текущем порядке" — как БД вернула БЕЗ ORDER BY (может отличаться
    от id ASC, хотя scripts/backfill_property_ids.py в проде ВСЕГДА
    добавляет ORDER BY id — см. докстринг верхнего уровня; тестируем оба
    как отдельные случаи, раз задача просит явно)."""
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    id_asc = sorted(rows, key=lambda r: r["id"])
    id_desc = list(reversed(id_asc))
    listed_asc = sorted(rows, key=lambda r: r["first_seen"] or epoch)
    listed_desc = list(reversed(listed_asc))
    shuffled_1 = list(rows)
    random.Random(42).shuffle(shuffled_1)
    shuffled_2 = list(rows)
    random.Random(1337).shuffle(shuffled_2)
    return {
        "current": rows,
        "listing_id_asc": id_asc,
        "listing_id_desc": id_desc,
        "listed_at_asc": listed_asc,
        "listed_at_desc": listed_desc,
        "shuffled_seed_42": shuffled_1,
        "shuffled_seed_1337": shuffled_2,
    }


# ── Симуляция линковщика (зеркалит bot/identity/property_linker.py) ────

@dataclass
class SimCluster:
    sim_id: int
    address_hash: str
    complex_id: int | None
    floor: int | None
    anchor_area: float | None
    anchor_rooms: int | None
    anchor_house_number: str | None
    members: list[dict] = field(default_factory=list)


def _build_fuzzy_event(row: dict, cluster: SimCluster, diff: float, candidates_count: int) -> dict:
    """Полная запись на один fuzzy match — задача, п.1: все запрошенные
    поля + "какие признаки совпали/различаются". НЕ включает телефон
    (такой колонки и нет), сырой address/title/description/url —
    только normalize_address()/house_number (задача: "не выводить...
    персональные данные")."""
    anchor = cluster.members[0]
    row_hn = extract_house_number(row.get("address"))

    rooms_known = row.get("rooms") is not None and cluster.anchor_rooms is not None
    rooms_match = rooms_known and row.get("rooms") == cluster.anchor_rooms

    hn_known = row_hn is not None and cluster.anchor_house_number is not None
    hn_match = hn_known and row_hn == cluster.anchor_house_number

    row_seller = _normalize_name(row["seller_name"]) if row.get("seller_name") else None
    anchor_seller = _normalize_name(anchor["seller_name"]) if anchor.get("seller_name") else None
    seller_known = row_seller is not None and anchor_seller is not None
    seller_match = seller_known and row_seller == anchor_seller

    overlap = _active_overlap(row, anchor)

    matched_features, differing_features = ["complex_id", "floor"], []
    matched_features.append(f"area(±{diff:.2f}м²)")
    if rooms_known:
        (matched_features if rooms_match else differing_features).append("rooms")
    else:
        differing_features.append("rooms(неизвестно у одной из сторон)")
    if hn_known:
        (matched_features if hn_match else differing_features).append("house_number")
    else:
        differing_features.append("house_number(неизвестен у одной из сторон)")
    if seller_known:
        (matched_features if seller_match else differing_features).append("seller_identity")
    else:
        differing_features.append("seller_identity(неизвестна у одной из сторон)")

    return {
        "listing_id": row["id"],
        "cluster_sim_id": cluster.sim_id,
        "cluster_anchor_listing_id": anchor["id"],
        "cluster_size_before_this_match": len(cluster.members),
        "complex_id": cluster.complex_id,
        "floor": cluster.floor,
        "normalized_address": normalize_address(row.get("address")),
        "anchor_normalized_address": normalize_address(anchor.get("address")),
        "house_number": row_hn,
        "anchor_house_number": cluster.anchor_house_number,
        "rooms": row.get("rooms"), "anchor_rooms": cluster.anchor_rooms,
        "area": row.get("area"), "anchor_area": cluster.anchor_area, "area_diff": round(diff, 3),
        "seller_name": row.get("seller_name"), "anchor_seller_name": anchor.get("seller_name"),
        "first_seen": row.get("first_seen"), "last_seen": row.get("last_seen"),
        "archived_at": row.get("archived_at"), "is_active": row.get("is_active"),
        "price": row.get("price"),
        "lat": row.get("lat"), "lon": row.get("lon"),
        "matched_features": matched_features,
        "differing_features": differing_features,
        "candidates_count": candidates_count,
        "confidence": _fuzzy_confidence(row.get("area"), cluster.anchor_area),
        "simultaneously_active": overlap,
    }


def simulate_linking(rows: list[dict], rule: RuleConfig = RULES["A_baseline"]) -> dict:
    """Возвращает {"stats", "assignments" (listing_id -> sim_id|None),
    "clusters" (address_hash -> SimCluster), "fuzzy_events",
    "ambiguous_candidate_attempts"}. Чистая функция, без БД/сети —
    полностью в памяти (см. докстринг модуля про мотивацию)."""
    by_hash: dict[str, SimCluster] = {}
    by_complex_floor: dict[tuple, list[SimCluster]] = defaultdict(list)
    next_id = 1
    stats = {"total": 0, "auto_new": 0, "auto_existing": 0, "fuzzy": 0, "skipped": 0}
    assignments: dict[str, int | None] = {}
    fuzzy_events: list[dict] = []
    ambiguous_candidate_attempts = 0

    for row in rows:
        stats["total"] += 1
        address_hash = compute_address_hash(row.get("address"), row.get("floor"), row.get("area"))
        if address_hash is None:
            stats["skipped"] += 1
            assignments[row["id"]] = None
            continue

        if address_hash in by_hash:
            cluster = by_hash[address_hash]
            cluster.members.append(row)
            stats["auto_existing"] += 1
            assignments[row["id"]] = cluster.sim_id
            continue

        complex_id, floor, area = row.get("complex_id"), row.get("floor"), row.get("area")
        matched, diff, candidates_count = None, None, 0

        if complex_id is not None and floor is not None and area is not None:
            bucket = by_complex_floor.get((complex_id, floor), [])
            qualifying = [(c, abs(c.anchor_area - area)) for c in bucket
                          if c.anchor_area is not None and abs(c.anchor_area - area) <= rule.area_tolerance]
            if rule.require_rooms_match:
                qualifying = [(c, d) for c, d in qualifying
                              if c.anchor_rooms is not None and row.get("rooms") is not None
                              and c.anchor_rooms == row.get("rooms")]
            if rule.require_house_number_match:
                row_hn = extract_house_number(row.get("address"))
                qualifying = [(c, d) for c, d in qualifying
                              if c.anchor_house_number is not None and row_hn is not None
                              and c.anchor_house_number == row_hn]
            candidates_count = len(qualifying)
            if candidates_count > 1:
                ambiguous_candidate_attempts += 1
            if candidates_count >= 1 and not (rule.single_candidate_only and candidates_count > 1):
                qualifying.sort(key=lambda cd: cd[1])
                matched, diff = qualifying[0]

        if matched is not None:
            fuzzy_events.append(_build_fuzzy_event(row, matched, diff, candidates_count))
            matched.members.append(row)
            stats["fuzzy"] += 1
            assignments[row["id"]] = matched.sim_id
            continue

        cluster = SimCluster(next_id, address_hash, complex_id, floor, area, row.get("rooms"),
                              extract_house_number(row.get("address")))
        cluster.members.append(row)
        by_hash[address_hash] = cluster
        if complex_id is not None and floor is not None and area is not None:
            by_complex_floor[(complex_id, floor)].append(cluster)
        assignments[row["id"]] = cluster.sim_id
        next_id += 1
        stats["auto_new"] += 1

    return {
        "stats": stats, "assignments": assignments, "clusters": by_hash,
        "fuzzy_events": fuzzy_events, "ambiguous_candidate_attempts": ambiguous_candidate_attempts,
    }


def _active_overlap(a: dict, b: dict) -> bool | None:
    """Были ли a/b РЕАЛЬНО одновременно "на рынке" (интервал [first_seen,
    archived_at или "по сей день"]) — НЕ просто "оба сейчас is_active"
    (это не отвечает на вопрос про историю), а пересечение интервалов
    экспозиции. None — недостаточно дат для ответа."""
    a_start, b_start = a.get("first_seen"), b.get("first_seen")
    if a_start is None or b_start is None:
        return None
    a_end = a.get("archived_at") or datetime.now(timezone.utc)
    b_end = b.get("archived_at") or datetime.now(timezone.utc)
    return a_start <= b_end and b_start <= a_end


# ── Точка 2: агрегаты неоднозначности ───────────────────────────────────

_AREA_DIFF_BUCKETS = [("exact", 0.0, 0.0), ("0-0.25", 0.0, 0.25),
                       ("0.25-0.5", 0.25, 0.5), ("0.5-1.0", 0.5, 1.0)]


def aggregate_ambiguity(sim: dict) -> dict:
    events = sim["fuzzy_events"]
    total = len(events)

    single_candidate = sum(1 for e in events if e["candidates_count"] <= 1)
    multi_candidate = total - single_candidate

    area_diff_dist = {label: 0 for label, _, _ in _AREA_DIFF_BUCKETS}
    for e in events:
        d = e["area_diff"]
        if d <= 0.0:
            area_diff_dist["exact"] += 1
        elif d <= 0.25:
            area_diff_dist["0-0.25"] += 1
        elif d <= 0.5:
            area_diff_dist["0.25-0.5"] += 1
        else:
            area_diff_dist["0.5-1.0"] += 1

    rooms_mismatch = sum(1 for e in events if "rooms" in e["differing_features"])
    address_mismatch = sum(1 for e in events if "house_number" in e["differing_features"])
    seller_mismatch = sum(1 for e in events if "seller_identity" in e["differing_features"])
    simultaneously_active = sum(1 for e in events if e["simultaneously_active"] is True)

    # Cluster-level: размеры, максимальный, clusters с overlap, ЖК-частота.
    clusters = sim["clusters"]
    size_buckets = {"2": 0, "3-5": 0, "6-10": 0, ">10": 0}
    max_cluster_size = 0
    clusters_with_simultaneous_active = 0
    complex_fuzzy_counts: dict[int, int] = defaultdict(int)

    for e in events:
        if e["complex_id"] is not None:
            complex_fuzzy_counts[e["complex_id"]] += 1

    for c in clusters.values():
        n = len(c.members)
        max_cluster_size = max(max_cluster_size, n)
        if n >= 2:
            if n == 2:
                size_buckets["2"] += 1
            elif n <= 5:
                size_buckets["3-5"] += 1
            elif n <= 10:
                size_buckets["6-10"] += 1
            else:
                size_buckets[">10"] += 1
            # Пересечение экспозиции хотя бы одной пары внутри кластера.
            has_overlap = False
            for i in range(len(c.members)):
                for j in range(i + 1, len(c.members)):
                    if _active_overlap(c.members[i], c.members[j]) is True:
                        has_overlap = True
                        break
                if has_overlap:
                    break
            if has_overlap:
                clusters_with_simultaneous_active += 1

    top_complexes = sorted(complex_fuzzy_counts.items(), key=lambda kv: kv[1], reverse=True)[:20]

    return {
        "fuzzy_total": total,
        "single_candidate_matches": single_candidate,
        "multi_candidate_matches": multi_candidate,
        "area_diff_distribution": area_diff_dist,
        "rooms_mismatch_count": rooms_mismatch,
        "address_house_number_mismatch_count": address_mismatch,
        "different_seller_identity_count": seller_mismatch,
        "simultaneously_active_pairs_count": simultaneously_active,
        "cluster_size_distribution": size_buckets,
        "max_cluster_size": max_cluster_size,
        "clusters_with_simultaneous_active_members": clusters_with_simultaneous_active,
        "top_complexes_by_fuzzy_usage": [{"complex_id": cid, "fuzzy_count": n} for cid, n in top_complexes],
    }


# ── Точка 3: устойчивость к порядку ─────────────────────────────────────

def compare_order_sensitivity(rows: list[dict]) -> dict:
    """Прогоняет simulate_linking (RULES["A_baseline"]) по всем order_
    variants() и сравнивает по СОДЕРЖАНИЮ (frozenset соклиентов на
    listing_id), НЕ по числовым sim_id — sim_id сам по себе порядково-
    зависимая нумерация (кто первый обработан, тот и получил меньший id),
    сравнивать по нему было бы категорической ошибкой (100% "различие"
    почти всегда, даже если кластеры идентичны по составу)."""
    variants = order_variants(rows)
    results = {name: simulate_linking(r, RULES["A_baseline"]) for name, r in variants.items()}

    def _clustermates(sim: dict) -> dict[str, frozenset]:
        assignments = sim["assignments"]
        by_sim_id: dict[int, list[str]] = defaultdict(list)
        for lid, sid in assignments.items():
            if sid is not None:
                by_sim_id[sid].append(lid)
        out = {}
        for lid, sid in assignments.items():
            if sid is None:
                out[lid] = None
            else:
                out[lid] = frozenset(x for x in by_sim_id[sid] if x != lid)
        return out

    reference_name = "current"
    reference = _clustermates(results[reference_name])

    comparison = {}
    for name, sim in results.items():
        stats = sim["stats"]
        changed = 0
        if name != reference_name:
            mates = _clustermates(sim)
            for lid, ref_set in reference.items():
                if mates.get(lid) != ref_set:
                    changed += 1
        comparison[name] = {
            "properties_created": stats["auto_new"],
            "fuzzy": stats["fuzzy"],
            "auto_existing": stats["auto_existing"],
            "skipped": stats["skipped"],
            "assignments_changed_vs_current": changed if name != reference_name else 0,
        }
    return comparison


# ── Точка 4: risk flags + детерминированная выборка ─────────────────────

def score_risk(e: dict) -> tuple[str, list[str]]:
    """low/medium/high + явные причины — ЭВРИСТИКА, НЕ заявление "это
    ошибка" или "это верное совпадение" (задача, п.4: "без объявления
    результата истинным совпадением"). Комбинирует независимые сигналы:
    неоднозначность кандидатов, явные несовпадения rooms/house_number,
    разная seller identity + одновременная активность (сильнее вместе,
    чем порознь — совпадение двух живых объявлений разных продавцов в
    одном пятне этаж+ЖК+площадь — классический профиль ДВУХ РЕАЛЬНЫХ
    квартир, не одной перевыставленной)."""
    reasons = []
    high = False
    medium = False

    if e["candidates_count"] > 1:
        high = True
        reasons.append(f"{e['candidates_count']} кандидатов в допуске одновременно — выбор не однозначен")
    if "rooms" in e["differing_features"]:
        high = True
        reasons.append(f"rooms не совпадают ({e['rooms']} vs {e['anchor_rooms']})")
    if "house_number" in e["differing_features"]:
        high = True
        reasons.append(f"номер дома не совпадает ({e['house_number']} vs {e['anchor_house_number']})")
    if e["simultaneously_active"] is True and "seller_identity" in e["differing_features"]:
        high = True
        reasons.append("разные seller identity И объявления пересекались по времени активности")
    elif e["simultaneously_active"] is True:
        medium = True
        reasons.append("объявления пересекались по времени активности (один продавец)")
    elif "seller_identity" in e["differing_features"]:
        medium = True
        reasons.append("разные seller identity (без подтверждённого пересечения активности)")
    if e["area_diff"] > 0.5:
        medium = True
        reasons.append(f"area_diff {e['area_diff']:.2f}м² — ближе к границе допуска, чем к центру")
    if "rooms(неизвестно у одной из сторон)" in e["differing_features"]:
        reasons.append("rooms неизвестны хотя бы у одной стороны — не подтверждает и не опровергает")
    if "house_number(неизвестен у одной из сторон)" in e["differing_features"]:
        reasons.append("номер дома не удалось извлечь хотя бы у одной стороны")

    if high:
        return "high", reasons
    if medium:
        return "medium", reasons
    if not reasons:
        reasons.append("единственный кандидат, area_diff ≤0.5, rooms/house_number совпадают "
                        "(где известны), не пересекались по активности")
    return "low", reasons


def build_review_sample(events: list[dict], n_per_bucket: int = 50) -> dict:
    """Детерминированная выборка (задача, п.4): 50 самых уверенных
    (наименьший area_diff), 50 у границы (area_diff ближе всего к
    tolerance), 50 из multi-seller кластеров, 50 из кластеров с
    одновременно активными listings. Пересечения между корзинами
    возможны и это нормально (задача не просит их исключать) —
    сортировка везде по (ключ, listing_id) для полной детерминированности."""
    by_confidence = sorted(events, key=lambda e: (e["area_diff"], e["listing_id"]))[:n_per_bucket]
    by_boundary = sorted(events, key=lambda e: (-e["area_diff"], e["listing_id"]))[:n_per_bucket]
    multi_seller = sorted(
        [e for e in events if "seller_identity" in e["differing_features"]],
        key=lambda e: (e["area_diff"], e["listing_id"]),
    )[:n_per_bucket]
    simultaneous = sorted(
        [e for e in events if e["simultaneously_active"] is True],
        key=lambda e: (e["area_diff"], e["listing_id"]),
    )[:n_per_bucket]

    def _tag(bucket_events, bucket_name):
        out = []
        for e in bucket_events:
            risk, reasons = score_risk(e)
            out.append({**e, "sample_bucket": bucket_name, "risk": risk, "risk_reasons": reasons})
        return out

    sample = {
        "most_confident": _tag(by_confidence, "most_confident"),
        "near_boundary": _tag(by_boundary, "near_boundary"),
        "multi_seller_identity": _tag(multi_seller, "multi_seller_identity"),
        "simultaneously_active": _tag(simultaneous, "simultaneously_active"),
    }
    all_tagged = {e["listing_id"]: e for bucket in sample.values() for e in bucket}
    top50 = sorted(
        all_tagged.values(),
        key=lambda e: ({"high": 0, "medium": 1, "low": 2}[e["risk"]], -e["candidates_count"],
                        -e["area_diff"], e["listing_id"]),
    )[:50]
    return {"sample": sample, "top50_most_suspicious": top50,
            "sample_size_total": len(all_tagged)}


# ── Точка 5: сравнение правил A-F ───────────────────────────────────────

def compare_rules(rows: list[dict]) -> dict:
    out = {}
    baseline_stats = None
    for name, rule in RULES.items():
        sim = simulate_linking(rows, rule)
        events = sim["fuzzy_events"]
        high_risk = sum(1 for e in events if score_risk(e)[0] == "high")
        row = {
            "auto_new": sim["stats"]["auto_new"],
            "auto_existing": sim["stats"]["auto_existing"],
            "fuzzy": sim["stats"]["fuzzy"],
            "skipped": sim["stats"]["skipped"],
            "ambiguous_candidate_attempts": sim["ambiguous_candidate_attempts"],
            "high_risk_matches": high_risk,
        }
        if name == "A_baseline":
            baseline_stats = row
        out[name] = row
    for name, row in out.items():
        row["delta_vs_baseline"] = {k: row[k] - baseline_stats[k] for k in
                                     ("auto_new", "fuzzy", "ambiguous_candidate_attempts", "high_risk_matches")}
    return out


# ── Точка 6: транзитивные цепочки ───────────────────────────────────────

def find_transitive_chains(sim: dict, rule: RuleConfig = RULES["A_baseline"]) -> list[dict]:
    """Кластеры, где max(area)-min(area) > rule.area_tolerance — возможны
    ТОЛЬКО через общий anchor (см. докстринг модуля: anchor area
    зафиксирован в момент создания и НИКОГДА не обновляется реальным
    линковщиком — как и здесь), поэтому это не "баг сравнения", а
    реальное свойство алгоритма: два member'а сами по себе могут быть
    вне tolerance друг от друга, если оба независимо попали в допуск
    ОТ ANCHOR'а, но не друг от друга (50.0 anchor -> 50.9 подошёл (0.9),
    49.6 подошёл отдельно (0.6) -> span 50.9-49.6=1.3 > 1.0)."""
    chains = []
    for c in sim["clusters"].values():
        areas = [m.get("area") for m in c.members if m.get("area") is not None]
        if len(areas) < 2:
            continue
        span = max(areas) - min(areas)
        if span > rule.area_tolerance:
            chains.append({
                "cluster_sim_id": c.sim_id, "complex_id": c.complex_id, "floor": c.floor,
                "anchor_area": c.anchor_area, "area_span": round(span, 3),
                "member_count": len(c.members),
                "member_listing_ids": [m["id"] for m in c.members],
                "member_areas": [m.get("area") for m in c.members],
            })
    return sorted(chains, key=lambda c: c["area_span"], reverse=True)


# ── Оркестрация ──────────────────────────────────────────────────────────

def _json_default(o):
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, frozenset):
        return list(o)
    return str(o)


async def run_audit() -> dict:
    rows = await _load_rows()
    baseline = simulate_linking(rows, RULES["A_baseline"])

    return {
        "loaded_rows": len(rows),
        "baseline_stats": baseline["stats"],
        "ambiguity": aggregate_ambiguity(baseline),
        "order_sensitivity": compare_order_sensitivity(rows),
        "review_sample": build_review_sample(baseline["fuzzy_events"]),
        "rule_comparison": compare_rules(rows),
        "transitive_chains": find_transitive_chains(baseline, RULES["A_baseline"]),
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples-out", default="audit_property_linker_fuzzy_samples.log",
                     help="куда записать полные записи выборки/top-50/цепочек (JSON, *.log паттерн gitignore)")
    args = ap.parse_args()

    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    try:
        result = await run_audit()
        with open(args.samples_out, "w", encoding="utf-8") as f:
            json.dump({
                "review_sample": result["review_sample"],
                "transitive_chains": result["transitive_chains"],
            }, f, ensure_ascii=False, indent=2, default=_json_default)

        summary = {
            "loaded_rows": result["loaded_rows"],
            "baseline_stats": result["baseline_stats"],
            "ambiguity": result["ambiguity"],
            "order_sensitivity": result["order_sensitivity"],
            "rule_comparison": result["rule_comparison"],
            "transitive_chains_count": len(result["transitive_chains"]),
            "top5_transitive_chains": result["transitive_chains"][:5],
            "review_sample_size": result["review_sample"]["sample_size_total"],
            "samples_written_to": args.samples_out,
        }
        log.info("ИТОГ: %s", json.dumps(summary, ensure_ascii=False, default=_json_default))
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default))
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
