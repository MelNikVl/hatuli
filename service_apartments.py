#!/usr/bin/env python3
"""
Сервис парсинга КВАРТИР НА ПРОДАЖУ.
Каждые 10-20 минут:
  1. Парсит krisha.kz/prodazha/kvartiry/astana/
  2. Считает скор (yield из rental_index, bargain из аналогов)
  3. Сохраняет/обновляет apartment_listings в PostgreSQL
  4. Синкает в Google Sheets (вкладка Квартиры)

Запуск:  python service_apartments.py
Логи:    apartments.log
"""
import asyncio
import json
import logging
import os
import random

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("apartments.log", encoding="utf-8", errors="replace"),
    ],
)
log = logging.getLogger("apartment_service")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")


async def run_cycle():
    from bot.core.apartment_parser import analyze_apartments
    from bot.core.sheets_sync import sync_apartments_to_sheets_pg
    from bot.db.pg import execute as pg_exec, fetchrow as pg_get
    from bot.db import settings as app_settings

    log.info("=== Apartment cycle start ===")

    # Подтягиваем настройки, выставленные через веб-терминал (/admin/settings)
    await app_settings.load()
    max_pages = app_settings.get_int("PARSER_MAX_PAGES", 5)

    results = await analyze_apartments("astana", max_pages=max_pages)
    log.info("Parsed %d listings", len(results))

    new_cnt = upd_cnt = 0

    for r in results:
        sd = r.get("score_data", {})
        bd = sd.get("breakdown", {})
        bargain = sd.get("bargain", {})
        reasons_json = json.dumps(sd.get("reasons", []), ensure_ascii=False)

        exists = await pg_get("SELECT id, price FROM apartment_listings WHERE id=$1", r["id"])

        try:
            if not exists:
                await pg_exec("""
                    INSERT INTO apartment_listings
                        (id, url, title, price, area, rooms, address, district, complex_name,
                         est_rent, yield_pct, payback_years,
                         score_total, score_yield, score_price_market, score_location,
                         score_apt_type, score_floor, score_complex, score_supply,
                         reasons, description, floor, floors_total,
                         year_built, building_type, renovation, furniture,
                         is_new_build, developer_name, seller_type, is_owner,
                         rent_source, bargain_target, bargain_discount_pct, bargain_rec,
                         details_fetched, first_seen, last_seen, notified)
                    VALUES
                        ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,
                         $13,$14,$15,$16,$17,$18,$19,$20,
                         $21,$22,$23,$24,$25,$26,$27,$28,$29,$30,
                         $31,$32,$33,$34,$35,$36,$37,NOW(),NOW(),FALSE)
                    ON CONFLICT (id) DO NOTHING
                """,
                    r["id"], r.get("url"), r.get("title"), r.get("price"), r.get("area"),
                    r.get("rooms"), r.get("address"), r.get("district"), r.get("complex_name"),
                    r.get("est_rent", 0), r.get("yield_pct", 0), r.get("payback_years"),
                    sd.get("total_score", 0), bd.get("yield", 0), bd.get("price_market", 0),
                    bd.get("location", 0), bd.get("apt_type", 0), bd.get("floor", 0),
                    bd.get("complex", 0), bd.get("supply", 0),
                    reasons_json, r.get("description", ""),
                    r.get("floor"), r.get("floors_total"),
                    r.get("year_built"), r.get("building_type"),
                    r.get("renovation"), r.get("furniture"),
                    r.get("is_new_build", False), r.get("developer_name"),
                    r.get("seller_type"), r.get("is_owner"),
                    r.get("rent_source"), bargain.get("target_price"),
                    bargain.get("discount_pct"), bargain.get("recommendation"),
                    r.get("details_fetched", False),
                )
                new_cnt += 1
            else:
                # История цен: фиксируем изменение — сигнал для алертов о торге
                old_price = exists["price"]
                new_price = r.get("price")
                if new_price and old_price and new_price != old_price:
                    try:
                        await pg_exec(
                            "INSERT INTO price_history (listing_id, old_price, new_price) "
                            "VALUES ($1, $2, $3)",
                            r["id"], old_price, new_price,
                        )
                        log.info("price change %s: %s -> %s", r["id"], old_price, new_price)
                    except Exception as e:
                        log.warning("price_history insert failed %s: %s", r["id"], e)
                await pg_exec("""
                    UPDATE apartment_listings SET
                        price=$2, est_rent=$3, yield_pct=$4, payback_years=$5,
                        score_total=$6, score_yield=$7, score_price_market=$8,
                        score_location=$9, score_apt_type=$10, score_floor=$11,
                        score_complex=$12, score_supply=$13, reasons=$14,
                        floor=$15, floors_total=$16, year_built=$17,
                        complex_name=$18, seller_type=$19, is_owner=$20,
                        rent_source=$21, bargain_target=$22,
                        bargain_discount_pct=$23, bargain_rec=$24,
                        details_fetched=$25, last_seen=NOW()
                    WHERE id=$1
                """,
                    r["id"], r.get("price"), r.get("est_rent", 0),
                    r.get("yield_pct", 0), r.get("payback_years"),
                    sd.get("total_score", 0), bd.get("yield", 0), bd.get("price_market", 0),
                    bd.get("location", 0), bd.get("apt_type", 0), bd.get("floor", 0),
                    bd.get("complex", 0), bd.get("supply", 0), reasons_json,
                    r.get("floor"), r.get("floors_total"), r.get("year_built"),
                    r.get("complex_name"), r.get("seller_type"), r.get("is_owner"),
                    r.get("rent_source"), bargain.get("target_price"),
                    bargain.get("discount_pct"), bargain.get("recommendation"),
                    r.get("details_fetched", False),
                )
                upd_cnt += 1
        except Exception as e:
            log.warning("DB error %s: %s", r["id"], e)

    log.info("DB: +%d new, ~%d updated", new_cnt, upd_cnt)

    # ── Отделка: определяем по тексту, правим скор ────────────────────────
    from bot.core.listing_intel import detect_finish_level
    for r in results:
        code, adj, label = detect_finish_level(r.get("title"), r.get("description"))
        if code:
            try:
                await pg_exec(
                    "UPDATE apartment_listings SET finish_level=$2, "
                    "score_total = LEAST(100, GREATEST(0, COALESCE(score_total,0) + $3)) "
                    "WHERE id=$1 AND (finish_level IS DISTINCT FROM $2)",
                    r["id"], code, adj,
                )
            except Exception as e:
                log.warning("finish update failed %s: %s", r["id"], e)

    # ── Координаты + свежепойманная архивность из детального парсера ──────
    for r in results:
        try:
            if r.get("lat") and r.get("lon"):
                await pg_exec(
                    "UPDATE apartment_listings SET lat=$2, lon=$3 WHERE id=$1",
                    r["id"], r["lat"], r["lon"],
                )
            if r.get("is_archived"):
                await pg_exec(
                    "UPDATE apartment_listings SET is_active=FALSE, archived_at=now() WHERE id=$1",
                    r["id"],
                )
        except Exception as e:
            log.warning("coords/archive update failed %s: %s", r["id"], e)

    # ── Зоны приоритета: пересчёт бонусов для всех объявлений с координатами ──
    try:
        from bot.core.zones import load_zones, zone_bonus_for
        zones = await load_zones()
        if zones:
            from bot.db.pg import fetch as pg_fetch
            coords = await pg_fetch(
                "SELECT id, lat, lon, COALESCE(zone_bonus,0) AS zb FROM apartment_listings "
                "WHERE lat IS NOT NULL AND lon IS NOT NULL"
            )
            zcnt = 0
            for c in coords:
                bonus, zname = zone_bonus_for(c["lat"], c["lon"], zones)
                if bonus != c["zb"]:
                    await pg_exec(
                        "UPDATE apartment_listings SET zone_bonus=$2, zone_name=$3 WHERE id=$1",
                        c["id"], bonus, zname,
                    )
                    zcnt += 1
            if zcnt:
                log.info("zones: updated bonus for %d listings", zcnt)
    except Exception as e:
        log.warning("zone recompute failed: %s", e)

    # ── Первичка/вторичка ─────────────────────────────────────────────────
    for r in results:
        blob = f"{r.get('title','')} {r.get('description','')}".lower()
        year = r.get("year_built")
        is_primary = (year and year >= 2026) or "от застройщика" in blob or "сдача в" in blob
        try:
            await pg_exec(
                "UPDATE apartment_listings SET market_type=$2 WHERE id=$1 AND market_type IS NULL",
                r["id"], "primary" if is_primary else "secondary",
            )
        except Exception as e:
            log.warning("market_type failed %s: %s", r["id"], e)

    # ── Слои скоринга (шум, школы — OSM с кешем) ──────────────────────────
    if app_settings.get_bool("OSM_LAYERS", True):
        try:
            from bot.score_layers import compute_all_layers, details_to_json
            from bot.db.pg import fetch as pg_fetch2
            candidates = await pg_fetch2("""
                SELECT id, lat, lon FROM apartment_listings
                WHERE lat IS NOT NULL AND lon IS NOT NULL
                  AND is_active IS NOT FALSE
                  AND (layers_computed_at IS NULL
                       OR layers_computed_at < now() - interval '30 days')
                ORDER BY score_total DESC NULLS LAST
                LIMIT 10
            """)
            for c in candidates:
                adj, details = await compute_all_layers(dict(c))
                await pg_exec(
                    "UPDATE apartment_listings SET layer_bonus=$2, "
                    "layer_details=$3::jsonb, layers_computed_at=now() WHERE id=$1",
                    c["id"], adj, details_to_json(details),
                )
                await asyncio.sleep(1.5)  # вежливость к Overpass
            if candidates:
                log.info("layers: computed for %d listings", len(candidates))
        except Exception as e:
            log.warning("layers failed: %s", e)

    # ── AI-анализ текста (DeepSeek, включается настройкой) ────────────────
    if app_settings.get_bool("AI_TEXT_ANALYSIS", False):
        try:
            from bot.core.ai_text_analysis import analyze_top_listings
            await analyze_top_listings(limit=10)
        except Exception as e:
            log.warning("ai text analysis failed: %s", e)

    # ── Проверка архивности топовых объявлений (максимум 15 за цикл) ──────
    try:
        from bot.core.archive_check import check_archived
        res = await check_archived(limit=15)
        log.info("archive check: %s", res)
    except Exception as e:
        log.warning("archive check failed: %s", e)

    # Google Sheets sync
    try:
        await sync_apartments_to_sheets_pg()
        log.info("Google Sheets: Квартиры synced")
        from datetime import datetime, timezone
        await app_settings.set("SHEETS_APARTMENTS_SYNCED_AT",
                               datetime.now(timezone.utc).isoformat())
    except Exception as e:
        log.warning("Sheets sync failed: %s", e)

    log.info("=== Apartment cycle done ===\n")


async def main():
    from bot.db.pg import init_pool
    await init_pool(DATABASE_URL)
    log.info("=== Apartment service started ===")

    # Первый цикл сразу
    try:
        await run_cycle()
    except Exception as e:
        log.error("First cycle error: %s", e, exc_info=True)

    while True:
        sleep_min = random.uniform(10, 20)
        log.info("Next cycle in %.0f min...", sleep_min)
        await asyncio.sleep(sleep_min * 60)
        try:
            await run_cycle()
        except Exception as e:
            log.error("Cycle error: %s", e, exc_info=True)


if __name__ == "__main__":
    asyncio.run(main())
