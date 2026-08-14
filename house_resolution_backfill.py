#!/usr/bin/env python3
"""Разовый re-attribution pass (задача 2026-08-13, "House-resolution в
матчинге apartment_listings", п.2) — все apartment_listings, лежащие на
зонтиках (complex_name = имя ЖК с детьми), пробуем резолвить к
конкретному дому по адресу/токену/гео (bot/core/house_resolution.py).

Отчёт: сколько уехало (resolved, по методам), сколько осталось
"дом неизвестен" (無 confidence — остались на зонтике, это ОЖИДАЕМЫЙ,
не ошибочный исход для части объявлений).

Запуск: venv/bin/python house_resolution_backfill.py [--dry]
"""
import argparse
import asyncio
import logging
import os

logger = logging.getLogger("house_resolution_backfill")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    from bot.db.pg import init_pool, close_pool, fetch
    from bot.core.house_resolution import get_umbrella_children, resolve_house

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        for line in open(".env"):
            if line.startswith("DATABASE_URL="):
                dsn = line.strip().split("=", 1)[1].strip('"').strip("'")
                break
    await init_pool(dsn)
    from bot.db.pg import execute

    umbrellas = await fetch("""
        SELECT DISTINCT parent_complex_id AS id FROM complexes
        WHERE parent_complex_id IS NOT NULL
    """)
    logger.info("зонтиков: %d", len(umbrellas))

    grand_total = 0
    grand_resolved = 0
    method_counts: dict[str, int] = {}
    per_umbrella_report = []

    for row in umbrellas:
        uid = row["id"]
        u = await fetch("SELECT id, name FROM complexes WHERE id=$1", uid)
        if not u:
            continue
        u = u[0]
        children = await get_umbrella_children(uid)
        if not children:
            continue
        listings = await fetch("""
            SELECT id, address, title, description, lat, lon
            FROM apartment_listings
            WHERE lower(trim(complex_name)) = lower(trim($1))
              AND resolved_house_id IS NULL
        """, u["name"])
        resolved_here = 0
        for l in listings:
            res = await resolve_house(
                umbrella_id=uid, umbrella_name=u["name"],
                listing_address=l["address"], listing_title=l["title"],
                listing_description=l["description"],
                listing_lat=l["lat"], listing_lon=l["lon"], children=children)
            grand_total += 1
            if res:
                resolved_here += 1
                grand_resolved += 1
                method_counts[res["method"]] = method_counts.get(res["method"], 0) + 1
                if not args.dry:
                    await execute(
                        "UPDATE apartment_listings SET resolved_house_id=$2, house_attribution=$3, "
                        "house_attribution_detail=$4 WHERE id=$1",
                        l["id"], res["house_id"], res["method"], res["detail"])
        unknown_here = len(listings) - resolved_here
        per_umbrella_report.append((uid, u["name"], len(children), len(listings), resolved_here, unknown_here))

    logger.info("=" * 70)
    logger.info("%s ОТЧЁТ ПО ЗОНТИКАМ", "[DRY] " if args.dry else "")
    for uid, name, n_children, total, resolved, unknown in per_umbrella_report:
        logger.info("  #%s %-30r домов=%-2d объявлений=%-4d уехало=%-4d дом неизвестен=%d",
                     uid, name, n_children, total, resolved, unknown)
    logger.info("=" * 70)
    logger.info("ИТОГО: %d объявлений проверено", grand_total)
    logger.info("  Уехало к дому: %d", grand_resolved)
    logger.info("  Дом неизвестен (остались на зонтике): %d", grand_total - grand_resolved)
    logger.info("  По методам: %s", method_counts)
    if args.dry:
        logger.info("DRY RUN — ничего не записано в БД")

    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
