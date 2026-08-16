#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ежедневный пересчёт seller_profiles — блок 7 docs/liquidity_model_design.md
(§2.7 «Продавец»), задача 2026-08-15, миграция 077.

`seller_type`/`seller_name` на КАЖДОМ объявлении уже были — этого скрипта
не было: агрегации на уровне продавца в проекте не существовало вообще
(§2.7 называла это «сильнейшим неиспользуемым сигналом ликвидности»).

Группировка — по нормализованному seller_name (trim + lower + схлопнутые
пробелы, см. _normalize_name()), НЕ по телефону: apartment_listings.
seller_phone не существует (телефон нигде не парсится, Krisha прячет его
за JS-кнопкой с отдельным AJAX) — решение подтверждено пользователем
2026-08-15, миграция 077 documents this в докстринге.

**Стоп-лист generic-имён** (_GENERIC_NAME_STOPLIST) — находка на живых
данных при разработке: "хозяин" — 1120 объявлений, "продавец" — 219,
"хозяйка" — 254 и т.д. Это НЕ один продавец с 1120 объявлениями, это
роль-заглушка тысяч разных людей, не указавших имя. Агрегировать их в
один "профиль продавца" было бы прямой ложью (мгновенно триггерило бы
"⚠️ агентство >50 активных объявлений" на КАЖДОМ таком объявлении) —
такие листинги пропускаются, seller_profiles на них не строится вовсе.

**Не решённая этим скриптом коллизия**: частые имена (Асель — 168
объявлений, Динара — 166 и т.п.) тоже почти наверняка разные реальные
люди, но в отличие от "хозяин"/"продавец" это осмысленные личные имена,
которые продавец мог выбрать сознательно (включая настоящих
высокообъёмных риелторов вроде "Ультаракова Асемгуль" — 75 объявлений,
один реальный человек). Без телефона отличить "10 разных Асель" от
"одного риелтора Асель с 10 объявлениями" нечем — честно снижаем
уверенность в UI-копирайтинге (bot/core/listing_detail.py), не делаем
вид, что проблемы нет.

Расписание: krisha-seller-profile.timer (ежедневно, ПОСЛЕ
krisha-listing-snapshot.timer 08:30 и krisha-outcome-labels.timer 08:45 —
relist_count читает outcome_labels.relisted_within_60d, нужны свежие
метки). Разовый прогон:
    venv/bin/python seller_profile_snapshot.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.FileHandler("seller_profile_snapshot.log", encoding="utf-8", errors="replace")],
)
log = logging.getLogger("seller_profile_snapshot")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

# Роль-заглушки, не имена — см. докстринг модуля. Ключи уже нормализованы
# (_normalize_name), точное совпадение (не substring — "Арман риелтор"
# несёт реальное имя "Арман", его агрегировать можно и нужно).
_GENERIC_NAME_STOPLIST = frozenset({
    "хозяин", "хозяйка", "хозяйн", "хозяева",
    "хозяин недвижимости", "хозяин квартиры", "хозяйка квартиры",
    "хозяин кв",
    "продавец", "владелец", "владелец квартиры",
    "собственник", "собственник недвижимости", "собственник квартиры",
    "seller", "owner", "user",
})

# Анти-шумовой порог поверх relist_rate>0.3 из задачи (см. докстринг
# migrations/077_seller_profiles.sql) — не отвечаем уверенно "часто
# перевыставляет" на выборке из 1-2 объявлений.
_MIN_LISTINGS_FOR_HIGH_RELIST = 3

# Порог "имя, вероятно, общее для нескольких разных людей" (миграция 079,
# находка на живых данных: 837 из 1249 срабатываний is_motivated_seller
# приходились именно на такие "имена" — см. докстринг миграции). >15
# объявлений под одним словом-именем (без телефона/др. идентификатора)
# на практике почти всегда коллизия, не один сверхактивный продавец.
_AMBIGUOUS_NAME_MIN_LISTINGS = 15
_HIGH_RELIST_THRESHOLD = 0.3
_MOTIVATED_SELLER_WINDOW_DAYS = 30
_MOTIVATED_SELLER_MIN_CUTS = 2


def _normalize_name(raw: str) -> str:
    """trim + lower + схлопнуть внутренние пробелы — тот же нормализатор
    что и в PK миграции 077, единый источник правды (используется и тут,
    и в тестах)."""
    return re.sub(r"\s+", " ", raw.strip()).lower()


async def _load_listing_base() -> list[dict]:
    """Одна строка на объявление с непустым seller_name — seller_type,
    активность, bargain_discount_pct (для median_discount_pct), outcome
    labels (relist/time_on_market) и property_id/даты property (для
    avg_true_dom_days — Property Identity, задача 2026-08-16, "P1 —
    Property Identity", пункт 6) уже присоединены — дальше группировка
    в Python (тот же паттерн, что complex_walkability_snapshot.py:
    таблицы небольшие по числу СУЩНОСТЕЙ (продавцы), полный проход
    дешевле, чем 8000+ отдельных запросов по продавцу).

    property_id/property_first_seen_at/property_last_seen_at — NULL, пока
    scripts/backfill_property_ids.py не отработал на этом listing_id
    (property_listings пуст до backfill'а — он не запускается
    автоматически, см. докстринг миграции 085) — честно, не гадаем."""
    from bot.db.pg import fetch
    rows = await fetch("""
        SELECT al.id, al.seller_name, al.seller_type, al.is_active, al.last_seen,
               al.bargain_discount_pct,
               ol.relisted_within_60d, ol.time_on_market,
               p.property_id, p.first_seen_at AS property_first_seen_at,
               p.last_seen_at AS property_last_seen_at
        FROM apartment_listings al
        LEFT JOIN outcome_labels ol ON ol.listing_id = al.id
        LEFT JOIN property_listings pl ON pl.listing_id = al.id
        LEFT JOIN properties p ON p.property_id = pl.property_id
        WHERE al.seller_name IS NOT NULL AND btrim(al.seller_name) != ''
    """)
    return [dict(r) for r in rows]


async def _load_price_cuts(listing_ids: set[str]) -> dict[str, list[dict]]:
    """listing_id -> список событий снижения цены (price_history, только
    new_price < old_price). Фильтр по listing_ids — Python-side set
    (продавцов без имени price_history тоже вернёт, но они не нужны:
    дешевле отфильтровать после одного полного чтения, чем городить
    JOIN/ANY($1::text[]) на потенциально большую price_history)."""
    from bot.db.pg import fetch
    rows = await fetch("""
        SELECT listing_id, old_price, new_price, changed_at
        FROM price_history
        WHERE new_price < old_price AND old_price > 0
    """)
    out: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r["listing_id"] in listing_ids:
            out[r["listing_id"]].append(dict(r))
    return out


def _aggregate(listings: list[dict], cuts_by_listing: dict[str, list[dict]],
               now: datetime) -> list[dict]:
    """Группировка по нормализованному имени + расчёт всех метрик
    seller_profiles. Возвращает список готовых строк для UPSERT."""
    by_seller: dict[str, list[dict]] = defaultdict(list)
    skipped_generic = 0
    for l in listings:
        name_norm = _normalize_name(l["seller_name"])
        if name_norm in _GENERIC_NAME_STOPLIST:
            skipped_generic += 1
            continue
        by_seller[name_norm].append(l)
    if skipped_generic:
        log.info("пропущено %d объявлений с generic-именем продавца (см. _GENERIC_NAME_STOPLIST)",
                  skipped_generic)

    motivated_cutoff = now - timedelta(days=_MOTIVATED_SELLER_WINDOW_DAYS)
    out = []
    for name_norm, group in by_seller.items():
        total = len(group)
        active = sum(1 for l in group if l["is_active"] is not False)

        # seller_type — у самого свежего (по last_seen) объявления с
        # непустым seller_type; NULL, если ни у одного не проставлен.
        typed = sorted((l for l in group if l.get("seller_type")),
                       key=lambda l: l["last_seen"] or now, reverse=True)
        seller_type = typed[0]["seller_type"] if typed else None

        relist_count = sum(1 for l in group if l.get("relisted_within_60d") is True)
        relist_rate = round(relist_count / total, 4) if total else None

        listing_ids = {l["id"] for l in group}
        cuts = [c for lid in listing_ids for c in cuts_by_listing.get(lid, [])]
        price_cut_count = sum(1 for lid in listing_ids if cuts_by_listing.get(lid))
        price_cut_rate = round(price_cut_count / total, 4) if total else None

        tom_values = [l["time_on_market"] for l in group if l.get("time_on_market") is not None]
        avg_days_to_sell = round(statistics.mean(tom_values), 1) if tom_values else None

        # avg_true_dom_days (Property Identity, задача 2026-08-16, "P1 —
        # Property Identity", пункт 6 — первое практическое применение
        # property_id) — по УНИКАЛЬНЫМ properties продавца, не по
        # отдельным listing_id: та же физическая квартира, перевыставленная
        # продавцом 3 раза, должна дать ОДИН срок экспозиции (первое
        # появление -> последнее), не три "новых с нуля" — ровно та
        # путаница, которую весь слой property_id устраняет. NULL, пока
        # backfill_property_ids.py не отработал на объявлениях этого
        # продавца (property_id у всех NULL до этого — не гадаем).
        seen_property_ids: set[int] = set()
        true_dom_values: list[int] = []
        for l in group:
            pid = l.get("property_id")
            if pid is None or pid in seen_property_ids:
                continue
            seen_property_ids.add(pid)
            first_at, last_at = l.get("property_first_seen_at"), l.get("property_last_seen_at")
            if first_at is not None and last_at is not None:
                true_dom_values.append((last_at - first_at).days)
        avg_true_dom_days = round(statistics.mean(true_dom_values), 1) if true_dom_values else None

        discount_values = [float(l["bargain_discount_pct"]) for l in group
                            if l.get("bargain_discount_pct") is not None]
        median_discount_pct = round(statistics.median(discount_values), 2) if discount_values else None

        is_high_relist_rate = bool(
            relist_rate is not None and relist_rate > _HIGH_RELIST_THRESHOLD
            and total >= _MIN_LISTINGS_FOR_HIGH_RELIST
        )
        recent_cuts = sum(1 for c in cuts if c["changed_at"] and c["changed_at"] >= motivated_cutoff)
        is_motivated_seller = recent_cuts >= _MOTIVATED_SELLER_MIN_CUTS

        # is_ambiguous (миграция 079) — >15 объявлений под одним словом-
        # именем почти наверняка разные реальные люди, не один продавец
        # (найдено на живых данных: 837/1249 срабатываний
        # is_motivated_seller были именно такими "именами"). Для
        # ambiguous ЖЁСТКО зануляем оба производных поведенческих флага
        # — сами числа (relist_rate/price_cut_rate) остаются как есть,
        # это честная агрегированная статистика по строке-имени, занулён
        # только вывод "этот ПРОДАВЕЦ так себя ведёт".
        is_ambiguous = total > _AMBIGUOUS_NAME_MIN_LISTINGS
        if is_ambiguous:
            is_high_relist_rate = False
            is_motivated_seller = False

        out.append({
            "seller_name": name_norm, "seller_type": seller_type,
            "active_listings_count": active, "total_listings_count": total,
            "relist_count": relist_count, "relist_rate": relist_rate,
            "price_cut_count": price_cut_count, "price_cut_rate": price_cut_rate,
            "avg_days_to_sell": avg_days_to_sell, "median_discount_pct": median_discount_pct,
            "avg_true_dom_days": avg_true_dom_days,
            "is_high_relist_rate": is_high_relist_rate, "is_motivated_seller": is_motivated_seller,
            "is_ambiguous": is_ambiguous,
        })
    return out


async def run_snapshot() -> dict:
    from bot.db.pg import execute

    listings = await _load_listing_base()
    listing_ids = {l["id"] for l in listings}
    cuts_by_listing = await _load_price_cuts(listing_ids)
    now = datetime.now(timezone.utc)
    profiles = _aggregate(listings, cuts_by_listing, now)

    for p in profiles:
        await execute("""
            INSERT INTO seller_profiles (
                seller_name, seller_type, active_listings_count, total_listings_count,
                relist_count, relist_rate, price_cut_count, price_cut_rate,
                avg_days_to_sell, median_discount_pct, avg_true_dom_days,
                is_high_relist_rate, is_motivated_seller, is_ambiguous, computed_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, now())
            ON CONFLICT (seller_name) DO UPDATE SET
                seller_type = EXCLUDED.seller_type,
                active_listings_count = EXCLUDED.active_listings_count,
                total_listings_count = EXCLUDED.total_listings_count,
                relist_count = EXCLUDED.relist_count,
                relist_rate = EXCLUDED.relist_rate,
                price_cut_count = EXCLUDED.price_cut_count,
                price_cut_rate = EXCLUDED.price_cut_rate,
                avg_days_to_sell = EXCLUDED.avg_days_to_sell,
                median_discount_pct = EXCLUDED.median_discount_pct,
                avg_true_dom_days = EXCLUDED.avg_true_dom_days,
                is_high_relist_rate = EXCLUDED.is_high_relist_rate,
                is_motivated_seller = EXCLUDED.is_motivated_seller,
                is_ambiguous = EXCLUDED.is_ambiguous,
                computed_at = now()
        """,
            p["seller_name"], p["seller_type"], p["active_listings_count"], p["total_listings_count"],
            p["relist_count"], p["relist_rate"], p["price_cut_count"], p["price_cut_rate"],
            p["avg_days_to_sell"], p["median_discount_pct"], p["avg_true_dom_days"],
            p["is_high_relist_rate"], p["is_motivated_seller"], p["is_ambiguous"],
        )

    # Продавцы, у которых сегодня не осталось ни одного объявления с
    # непустым/неgeneric именем (снятые с публикации давным-давно,
    # переименовавшиеся) — не актуальный профиль, не архив (тот же
    # принцип, что outcome_labels: только текущее состояние).
    current_names = {p["seller_name"] for p in profiles}
    deleted = await execute(
        "DELETE FROM seller_profiles WHERE seller_name != ALL($1::text[])",
        list(current_names) or [""],
    )

    high_relist = sum(1 for p in profiles if p["is_high_relist_rate"])
    motivated = sum(1 for p in profiles if p["is_motivated_seller"])
    big_agencies = sum(1 for p in profiles if p["active_listings_count"] > 50)
    ambiguous = sum(1 for p in profiles if p["is_ambiguous"])
    log.info("seller_profile_snapshot: продавцов=%d high_relist=%d motivated=%d "
             "агентств(>50 активных)=%d ambiguous(>%d объявлений)=%d удалено_устаревших=%s",
             len(profiles), high_relist, motivated, big_agencies,
             _AMBIGUOUS_NAME_MIN_LISTINGS, ambiguous, deleted)
    return {"sellers": len(profiles), "high_relist": high_relist, "motivated": motivated,
            "big_agencies": big_agencies, "ambiguous": ambiguous}


async def main() -> None:
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    try:
        result = await run_snapshot()
        print(result)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
