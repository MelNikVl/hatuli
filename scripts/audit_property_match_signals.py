#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""audit_property_match_signals.py — READ-ONLY аудит сильных сигналов
для property-матчинга (задача 2026-08-16, "Property Identity v2").
Прямое продолжение двух предыдущих read-only аудитов этой ветки задач:
    - scripts/audit_address_hash_exact.py (exact-hash кластеры)
    - scripts/audit_property_linker_fuzzy.py (fuzzy-кандидаты)
Этот скрипт их НЕ дублирует — импортирует и переиспользует их
группировку (group_by_exact_hash / simulate_linking), добавляя НОВЫЕ
сигналы (rooms/house_number/seller/фото/description/цена/координаты/
is_duplicate-cross-check) и классификацию по tier'ам (задача, п.4).

НИЧЕГО НЕ ПИШЕТ — только SELECT. НЕ трогает property_linker.py, схему,
production-данные.

Принцип (дан явно в предыдущей задаче, актуален и здесь): false
positive merge хуже false negative duplicate — поэтому tier'ы
объяснимы и НИКОГДА не называют пару "confirmed" (задача, п.4: "Не
использовать слово confirmed, пока нет номера квартиры, ручной
проверки или другого независимого ground truth").

Персональные данные (seller_name, сырой address, url) в агрегированный
вывод/top-примеры НЕ попадают — только булевы "совпадает/не совпадает"
и структурные поля (complex_id, floor, area_diff, tier).

Запуск:
    venv/bin/python scripts/audit_property_match_signals.py
"""
from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import logging
import math
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.FileHandler("audit_property_match_signals.log", encoding="utf-8", errors="replace")],
)
log = logging.getLogger("audit_property_match_signals")

from bot.identity.property_linker import compute_address_hash
from seller_profile_snapshot import _normalize_name
from audit_address_hash_exact import group_by_exact_hash
from audit_property_linker_fuzzy import simulate_linking, RULES, extract_house_number

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

# ── Пороги сигналов (эвристика, не откалибровано на ground truth —
# задача явно просит НЕ вводить thresholds на предыдущем шаге; здесь
# они локальны для tier-классификации ЭТОГО read-only аудита, не
# записываются никуда, не становятся прод-константами). ────────────
_PRICE_SIMILAR_PCT = 0.10
_PRICE_SEVERE_DIFF_PCT = 0.30
_DESC_SIMILAR_RATIO = 0.6
_COORDS_EQUAL_TOLERANCE_M = 50.0


async def _load_rows() -> dict[str, dict]:
    """listing_id -> полная строка (для обогащения пар, найденных
    group_by_exact_hash/simulate_linking — те возвращают ПОДМНОЖЕСТВО
    полей, этот словарь даёт доступ к description/photos/is_duplicate/
    duplicate_of/lat/lon/price/dup_match, которых там нет)."""
    from bot.db.pg import fetch
    lookup_rows = await fetch("SELECT id, name FROM complexes WHERE name IS NOT NULL ORDER BY id ASC")
    complex_lookup: dict[str, int] = {}
    for r in lookup_rows:
        key = r["name"].strip().lower()
        if key and key not in complex_lookup:
            complex_lookup[key] = r["id"]

    rows = await fetch("""
        SELECT id, address, floor, area, rooms, complex_name,
               first_seen, last_seen, archived_at, is_active,
               seller_name, price, lat, lon, description,
               photos, is_duplicate, duplicate_of, dup_match
        FROM apartment_listings
    """)
    out = {}
    for r in rows:
        d = dict(r)
        cn = d.get("complex_name")
        d["complex_id"] = complex_lookup.get(cn.strip().lower()) if cn else None
        out[d["id"]] = d
    return out


def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _active_overlap(a: dict, b: dict) -> bool | None:
    a_start, b_start = a.get("first_seen"), b.get("first_seen")
    if a_start is None or b_start is None:
        return None
    a_end = a.get("archived_at") or datetime.now(timezone.utc)
    b_end = b.get("archived_at") or datetime.now(timezone.utc)
    return a_start <= b_end and b_start <= a_end


def _photo_basenames(photos) -> set[str] | None:
    """URL-basename (файл без CDN-хоста/пути) — ближе к "тот же файл",
    чем полный URL (query-параметры/домен зеркал могут отличаться,
    сам UUID-сегмент — нет). None, если photos пуст/отсутствует."""
    if not photos:
        return None
    urls = photos if isinstance(photos, list) else json.loads(photos) if isinstance(photos, str) else None
    if not urls:
        return None
    out = set()
    for u in urls:
        m = re.search(r"([0-9a-f-]{20,})", u)
        if m:
            out.add(m.group(1))
    return out or None


def _price_signal(price_a: int | None, price_b: int | None) -> tuple[float | None, bool | None, bool | None]:
    if not price_a or not price_b:
        return None, None, None
    diff_pct = abs(price_a - price_b) / max(price_a, price_b)
    return round(diff_pct, 4), diff_pct <= _PRICE_SIMILAR_PCT, diff_pct > _PRICE_SEVERE_DIFF_PCT


def build_pair_evidence(a: dict, b: dict, pair_type: str, area_diff: float,
                         candidates_count: int = 1) -> dict:
    """Полная evidence-запись на пару (a=anchor/уже известный,
    b=новый/кандидат) — задача, п.3: все положительные и конфликтные
    сигналы. pair_type: 'exact_hash' | 'fuzzy_candidate'."""
    a_hn, b_hn = extract_house_number(a.get("address")), extract_house_number(b.get("address"))
    hn_known = a_hn is not None and b_hn is not None
    house_number_equal = (a_hn == b_hn) if hn_known else None

    rooms_known = a.get("rooms") is not None and b.get("rooms") is not None
    rooms_equal = (a["rooms"] == b["rooms"]) if rooms_known else None

    a_seller = _normalize_name(a["seller_name"]) if a.get("seller_name") else None
    b_seller = _normalize_name(b["seller_name"]) if b.get("seller_name") else None
    seller_known = a_seller is not None and b_seller is not None
    seller_equal = (a_seller == b_seller) if seller_known else None

    desc_a, desc_b = a.get("description"), b.get("description")
    desc_known = bool(desc_a) and bool(desc_b)
    desc_ratio = difflib.SequenceMatcher(None, desc_a, desc_b).ratio() if desc_known else None
    desc_similar = (desc_ratio >= _DESC_SIMILAR_RATIO) if desc_known else None

    photos_a, photos_b = _photo_basenames(a.get("photos")), _photo_basenames(b.get("photos"))
    photo_known = photos_a is not None and photos_b is not None
    photo_overlap = bool(photos_a & photos_b) if photo_known else None

    coords_known = all(v is not None for v in (a.get("lat"), a.get("lon"), b.get("lat"), b.get("lon")))
    coords_dist_m = _haversine_m(a["lat"], a["lon"], b["lat"], b["lon"]) if coords_known else None
    coords_equal = (coords_dist_m <= _COORDS_EQUAL_TOLERANCE_M) if coords_known else None

    price_diff_pct, price_similar, price_severe = _price_signal(a.get("price"), b.get("price"))

    overlap = _active_overlap(a, b)
    no_temporal_overlap = (overlap is False) if overlap is not None else None

    # is_duplicate/duplicate_of — независимый, уже ЖИВОЙ прод-сигнал
    # (bot/core/dedup_listings.py) — задача, "не создавать вторую
    # параллельную систему": сверяем с уже существующим механизмом,
    # не игнорируем его. TRUE — сильное независимое подтверждение.
    already_confirmed_by_dedup_listings = (
        a.get("duplicate_of") == b["id"] or b.get("duplicate_of") == a["id"]
        or (a.get("is_duplicate") and b.get("is_duplicate") and a.get("duplicate_of") == b.get("duplicate_of")
            and a.get("duplicate_of") is not None)
    )

    return {
        "listing_a": a["id"], "listing_b": b["id"], "pair_type": pair_type,
        "complex_id": a.get("complex_id"), "floor": a.get("floor"),
        "area_diff": round(area_diff, 3), "candidates_count": candidates_count,
        "rooms_a": a.get("rooms"), "rooms_b": b.get("rooms"), "rooms_equal": rooms_equal,
        "house_number_a": a_hn, "house_number_b": b_hn, "house_number_equal": house_number_equal,
        "seller_equal": seller_equal,
        "description_similarity": round(desc_ratio, 3) if desc_ratio is not None else None,
        "description_similar": desc_similar,
        "photo_overlap": photo_overlap,
        "coords_dist_m": round(coords_dist_m, 1) if coords_dist_m is not None else None,
        "coords_equal": coords_equal,
        "price_diff_pct": price_diff_pct, "price_similar": price_similar,
        "price_severely_different": price_severe,
        "simultaneously_active": overlap, "no_temporal_overlap": no_temporal_overlap,
        "already_confirmed_by_dedup_listings": already_confirmed_by_dedup_listings,
    }


_POSITIVE_SIGNAL_KEYS = (
    "rooms_equal", "house_number_equal", "seller_equal", "description_similar",
    "photo_overlap", "coords_equal", "price_similar", "no_temporal_overlap",
    "already_confirmed_by_dedup_listings",
)


def classify_tier(ev: dict) -> tuple[str, list[str]]:
    """rejected / weak_candidate / strong_candidate / review_required —
    задача, п.4. НИКОГДА 'confirmed' (см. докстринг модуля). Явные,
    воспроизводимые причины — не скрытый скор."""
    reasons = []

    # rejected — прямое структурное противоречие, перебивает всё
    # остальное (тот же принцип, что phase2_unit_match.py::decide_pair:
    # "номер и НЕ равны -> reject, перебивает остальные сигналы").
    if ev["rooms_equal"] is False:
        reasons.append(f"rooms не совпадают: {ev['rooms_a']} vs {ev['rooms_b']}")
    if ev["house_number_equal"] is False:
        reasons.append(f"номер дома не совпадает: {ev['house_number_a']} vs {ev['house_number_b']}")
    if ev["price_severely_different"] is True:
        reasons.append(f"цена отличается на {ev['price_diff_pct']*100:.0f}% (>{_PRICE_SEVERE_DIFF_PCT*100:.0f}%)")
    if reasons:
        return "rejected", reasons

    # Уже независимо подтверждено ДРУГИМ живым механизмом — сильнейший
    # положительный сигнал, сразу strong (задача: "можно ли считать
    # положительным доказательством" — да, это внешнее, не наше решение).
    if ev["already_confirmed_by_dedup_listings"]:
        return "strong_candidate", ["уже подтверждено bot/core/dedup_listings.py (is_duplicate/duplicate_of)"]

    positives = [k for k in _POSITIVE_SIGNAL_KEYS if ev.get(k) is True]
    conflicts = []
    if ev.get("simultaneously_active") is True:
        conflicts.append("объявления пересекались по времени активности")
    if ev.get("seller_equal") is False:
        conflicts.append("разные seller identity")

    strong_identity_signal = ev.get("house_number_equal") is True

    if not conflicts and strong_identity_signal and len(positives) >= 2:
        return "strong_candidate", [f"положительные сигналы: {positives}"]
    if conflicts:
        return "review_required", [f"конфликтующие сигналы: {conflicts}", f"положительные: {positives}"]
    if positives:
        return "weak_candidate", [f"положительные сигналы (без номера дома): {positives}"]
    return "review_required", ["недостаточно сигналов для решения"]


def build_exact_hash_pairs(rows: dict[str, dict]) -> list[dict]:
    """Все пары ВНУТРИ каждого exact-hash кластера размера 2+ (переиспользует
    audit_address_hash_exact.group_by_exact_hash — та же группировка,
    что уже используется предыдущим аудитом, не дублируем логику)."""
    row_list = list(rows.values())
    by_hash = group_by_exact_hash(row_list)
    pairs = []
    for h, members in by_hash.items():
        if len(members) < 2:
            continue
        anchor = members[0]
        for other in members[1:]:
            pairs.append(build_pair_evidence(anchor, other, "exact_hash", area_diff=0.0, candidates_count=1))
    return pairs


def build_fuzzy_candidate_pairs(rows: dict[str, dict]) -> list[dict]:
    """Anchor-кандидат пары из simulate_linking (переиспользует
    audit_property_linker_fuzzy.py — та же симуляция RULES['A_baseline'],
    не дублируем правило complex+floor+area±1м²)."""
    row_list = list(rows.values())
    sim = simulate_linking(row_list, RULES["A_baseline"])
    pairs = []
    for event in sim["fuzzy_events"]:
        anchor = rows.get(event["cluster_anchor_listing_id"])
        candidate_row = rows.get(event["listing_id"])
        if anchor is None or candidate_row is None:
            continue
        pairs.append(build_pair_evidence(
            anchor, candidate_row, "fuzzy_candidate",
            area_diff=event["area_diff"], candidates_count=event["candidates_count"],
        ))
    return pairs


def _tier_summary(pairs: list[dict]) -> dict:
    by_tier: dict[str, list[dict]] = defaultdict(list)
    for p in pairs:
        tier, reasons = classify_tier(p)
        p["tier"] = tier
        p["tier_reasons"] = reasons
        by_tier[tier].append(p)

    total = len(pairs)
    complex_by_tier: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    for tier, plist in by_tier.items():
        for p in plist:
            if p["complex_id"] is not None:
                complex_by_tier[tier][p["complex_id"]] += 1

    def _anonymize(p: dict) -> dict:
        return {
            "pair_type": p["pair_type"], "complex_id": p["complex_id"], "floor": p["floor"],
            "area_diff": p["area_diff"], "rooms_equal": p["rooms_equal"],
            "house_number_equal": p["house_number_equal"], "seller_equal": p["seller_equal"],
            "simultaneously_active": p["simultaneously_active"],
            "already_confirmed_by_dedup_listings": p["already_confirmed_by_dedup_listings"],
            "tier_reasons": p["tier_reasons"],
        }

    return {
        "total_pairs": total,
        "coverage_ratio": {tier: round(len(plist) / total, 4) if total else None for tier, plist in by_tier.items()},
        "counts": {tier: len(plist) for tier, plist in by_tier.items()},
        "top_complexes_by_tier": {
            tier: sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:10]
            for tier, counts in complex_by_tier.items()
        },
        "top10_examples_by_tier": {
            tier: [_anonymize(p) for p in plist[:10]] for tier, plist in by_tier.items()
        },
    }


async def run_audit() -> dict:
    rows = await _load_rows()
    exact_pairs = build_exact_hash_pairs(rows)
    fuzzy_pairs = build_fuzzy_candidate_pairs(rows)

    return {
        "loaded_listings": len(rows),
        "exact_hash_pairs": _tier_summary(exact_pairs),
        "fuzzy_candidate_pairs": _tier_summary(fuzzy_pairs),
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    args = ap.parse_args()

    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    try:
        result = await run_audit()
        summary = {
            "loaded_listings": result["loaded_listings"],
            "exact_hash": {
                "total_pairs": result["exact_hash_pairs"]["total_pairs"],
                "counts": result["exact_hash_pairs"]["counts"],
                "coverage_ratio": result["exact_hash_pairs"]["coverage_ratio"],
            },
            "fuzzy_candidate": {
                "total_pairs": result["fuzzy_candidate_pairs"]["total_pairs"],
                "counts": result["fuzzy_candidate_pairs"]["counts"],
                "coverage_ratio": result["fuzzy_candidate_pairs"]["coverage_ratio"],
            },
        }
        log.info("ИТОГ: %s", json.dumps(summary, ensure_ascii=False, default=str))
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
        with open("audit_property_match_signals_detail.log", "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
