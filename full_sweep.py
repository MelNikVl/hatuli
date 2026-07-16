#!/usr/bin/env python3
"""
ПОЛНЫЙ РАЗОВЫЙ ОБХОД всей выдачи продаж krisha.kz по Астане.

Зачем: постоянный «глубокий обход» в krisha-apartments добирает выдачу
по 5 страниц за цикл — на полный круг уходят дни, и если объявлений
не хватает, быстрее один раз пройти всё целиком этим скриптом.

Что делает:
  - идёт от страницы 1 до настоящего конца выдачи (успешно скачанная
    пустая страница), батчами по BATCH страниц;
  - БЕЗ ценового потолка (вся выдача, включая дорогие квартиры);
  - каждое объявление скорит тем же пайплайном, что и основной сервис,
    и вставляет/обновляет в apartment_listings;
  - позицию хранит в app_settings (FULL_SWEEP_PAGE) — при обрыве
    запускаешь снова, и он продолжит с места остановки;
  - паузы между страницами те же, что у парсера (2-5 сек) — Крышу не душим.

Запуск на сервере (лучше в tmux/screen, обход занимает часы):
    cd ~/krisha_bot && venv/bin/python full_sweep.py

Сброс позиции (начать с нуля):
    venv/bin/python full_sweep.py --reset
"""
import asyncio
import json
import logging
import os
import sys

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("full_sweep.log", encoding="utf-8", errors="replace"),
    ],
)
log = logging.getLogger("full_sweep")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")
BATCH = 5           # страниц за подход (между подходами — короткая пауза)
PAUSE_BETWEEN = 20  # сек между батчами


async def upsert(r: dict) -> str:
    """Вставка/обновление одного объявления (те же поля, что в service_apartments)."""
    from bot.db.pg import execute as pg_exec, fetchrow as pg_get

    sd = r.get("score_data", {})
    bd = sd.get("breakdown", {})
    bargain = sd.get("bargain", {})
    reasons_json = json.dumps(sd.get("reasons", []), ensure_ascii=False)

    exists = await pg_get("SELECT id, price FROM apartment_listings WHERE id=$1", r["id"])
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
        return "new"

    # уже есть — обновляем цену/скор/last_seen (лёгкий апдейт)
    if r.get("price") and exists["price"] and exists["price"] != r["price"]:
        try:
            await pg_exec(
                "INSERT INTO price_history (listing_id, old_price, new_price) VALUES ($1,$2,$3)",
                r["id"], exists["price"], r["price"])
        except Exception:
            pass
    await pg_exec("""
        UPDATE apartment_listings SET
            price=$2, est_rent=$3, yield_pct=$4,
            score_total=$5, last_seen=NOW()
        WHERE id=$1
    """, r["id"], r.get("price"), r.get("est_rent", 0),
         r.get("yield_pct", 0), sd.get("total_score", 0))
    return "upd"


async def main():
    from bot.db.pg import init_pool
    from bot.db import settings as app_settings
    from bot.core.apartment_parser import analyze_apartments

    await init_pool(DATABASE_URL)
    await app_settings.load()

    if "--reset" in sys.argv:
        await app_settings.set("FULL_SWEEP_PAGE", "1")
        log.info("Позиция сброшена — начинаем с 1-й страницы")

    page = app_settings.get_int("FULL_SWEEP_PAGE", 1)
    total_new = total_upd = 0
    log.info("=== Полный обход выдачи, старт со страницы %d ===", page)

    while True:
        stats: dict = {}
        try:
            results = await analyze_apartments(
                "astana", max_pages=BATCH, start_page=page,
                max_price=0,  # БЕЗ потолка — вся выдача
                stats=stats)
        except Exception as e:
            log.warning("Батч %d-%d упал: %s — пауза 60с и повтор",
                        page, page + BATCH - 1, e)
            await asyncio.sleep(60)
            continue

        pages_ok = stats.get("pages_ok", 0)
        pages_failed = stats.get("pages_failed", 0)
        reached_end = stats.get("reached_end", False)

        for r in results:
            try:
                if await upsert(r) == "new":
                    total_new += 1
                else:
                    total_upd += 1
            except Exception as e:
                log.warning("DB error %s: %s", r.get("id"), e)

        log.info("Страницы %d-%d: %d объявлений (ok=%d, fail=%d) | всего +%d новых, ~%d обновлено",
                 page, page + BATCH - 1, len(results), pages_ok, pages_failed,
                 total_new, total_upd)

        if reached_end:
            log.info("=== КОНЕЦ ВЫДАЧИ на ~стр. %d. Итог: +%d новых, ~%d обновлено ===",
                     page + pages_ok, total_new, total_upd)
            await app_settings.set("FULL_SWEEP_PAGE", "1")
            break

        if pages_ok == 0 and pages_failed > 0:
            log.warning("Все страницы батча упали — пауза 120с, повтор с той же позиции")
            await asyncio.sleep(120)
            continue

        page += pages_ok + pages_failed
        await app_settings.set("FULL_SWEEP_PAGE", str(page))
        await asyncio.sleep(PAUSE_BETWEEN)

    log.info("Готово. Скоринг/координаты/зоны добьёт основной сервис в своих циклах.")


if __name__ == "__main__":
    asyncio.run(main())
