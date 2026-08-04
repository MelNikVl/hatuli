"""
Фоновый сервис: привязка объявлений к ЖК/адресу/координатам.

Раньше все стадии этого пайплайна запускались вручную (кнопки в админке
или venv/bin/python complex_coords.py и т.п.), поэтому объявления подолгу
оставались без ЖК/координат — не потому что не находилось совпадение,
а потому что стадии просто никто не запускал регулярно.

Стадии за один цикл (все идемпотентны, повторный запуск безопасен):
  1. rebind        — bot.core.rebind.run_rebind (адрес/ЖК по ссылке/по
                      названию в заголовке/геопривязка к ближайшему ЖК)
  2. complex_audit  — bot.core.complex_audit.purge_street_complexes
                      (отлов псевдо-ЖК = улиц, отвязка их объявлений)
  3. complex_coords — complex_coords.py: координаты ЖК из центроидов
                      и с детальных страниц Korter/Homsters
  4. krisha_complex — krisha_complex_import.py: застройщик/адрес/год/
                      координаты ЖК со страницы Крыши (лимит за цикл —
                      это внешний скрейпинг с задержками 4-8с/запрос)
  5. geocode        — bot.core.rebind.geocode_missing_coords: Nominatim
                      фолбэк для объявлений с адресом, но без координат
                      (лимит батча — Nominatim 1 запрос/сек)

Разовая проверка (ничего не пишет во внешние источники, кроме БД):
    venv/bin/python service_geobind.py --once

Логирует в geobind.log (см. krisha-geobind.service).
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import sys

sys.path.insert(0, ".")

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("geobind_service")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

# Было раз в 6 часов (±1). Расследование 2026-08-04 (растущий график
# "непривязанные во времени"): бэклог "осталось без ЖК" рос ~50-70/час
# (темп новых объявлений после всплеска), а geocode-стадия резолвила лишь
# ~130 из 200 попыток раз в 6ч ≈ 22/час нетто — то есть цикл структурно не
# успевал даже за обычным притоком, не говоря о разборе самого бэклога
# (~9.9к на момент расследования). Подняли частоту цикла и объём пачки
# (см. GEOCODE_BATCH ниже) — цель ~90/час нетто, чтобы перегонять приток
# и начать реально сокращать бэклог, а не только держать его вровень.
MIN_INTERVAL_H = 3 - 0.5
MAX_INTERVAL_H = 3 + 0.5

# Быстрая привязка (только rebind — без внешних запросов, чистый SQL по своей
# БД) — отдельным более частым циклом, чтобы новые объявления от парсера
# (цикл ~60-80 мин) не копились непривязанными до следующего большого цикла
# раз в 6 часов. Без этого график /admin/unbound "пилит" вверх между циклами.
FAST_REBIND_INTERVAL_MIN = 20

# Лимиты внешних запросов за один цикл — самоограничение, чтобы не долбить
# krisha.kz/korter.kz/homsters.kz/nominatim слишком часто одним прогоном.
KRISHA_COMPLEX_LIMIT = 60      # ЖК за цикл (~4-8с/шт => до ~8 мин)
COMPLEX_COORDS_PAGES = 40      # страниц Korter/Homsters за цикл
GEOCODE_BATCH = 350            # адресов за цикл (Nominatim 1 req/s => ~6 мин; было 200, поднято 2026-08-04 — см. комментарий у MIN_INTERVAL_H)


async def run_cycle() -> None:
    from bot.core.rebind import run_rebind, geocode_missing_coords
    from bot.core.complex_audit import purge_street_complexes, backfill_year_built
    from bot.db.pg import execute, fetch

    log.info("=== Geobind cycle start ===")

    try:
        res = await run_rebind(progress_cb=lambda s: log.info("rebind: %s", s))
        log.info("rebind: bound=%d (url=%d text=%d geo=%d) addr_filled=%d left=%d",
                  res["bound"], res["by_url"], res["by_text"], res["by_geo"],
                  res["addr_filled"], res["left"])
    except Exception as e:
        log.error("rebind stage failed: %s", e, exc_info=True)

    try:
        res = await purge_street_complexes()
        log.info("complex_audit: flagged=%d unbound=%d coords_recomputed=%d",
                  res["flagged"], res["unbound"], res["coords_recomputed"])
    except Exception as e:
        log.error("complex_audit stage failed: %s", e, exc_info=True)

    try:
        n = await backfill_year_built()
        log.info("year_built backfilled: %d", n)
    except Exception as e:
        log.error("year_built backfill stage failed: %s", e, exc_info=True)

    try:
        from complex_coords import fill_from_centroids, fill_from_pages
        n1 = await fill_from_centroids(execute, test=False)
        n2 = await fill_from_pages(fetch, execute, pages=COMPLEX_COORDS_PAGES, test=False)
        log.info("complex_coords: centroids=%d pages=%d", n1, n2)
    except Exception as e:
        log.error("complex_coords stage failed: %s", e, exc_info=True)

    try:
        from krisha_complex_import import fetch_all, save_to_db
        found = await fetch_all(limit=KRISHA_COMPLEX_LIMIT)
        saved = await save_to_db(found) if found else 0
        log.info("krisha_complex_import: saved=%d/%d", saved, len(found))
    except Exception as e:
        log.error("krisha_complex_import stage failed: %s", e, exc_info=True)

    try:
        res = await geocode_missing_coords(
            progress_cb=lambda s: log.info("geocode: %s", s),
            batch_size=GEOCODE_BATCH,
        )
        log.info("geocode: attempted=%d geocoded=%d failed=%d",
                  res["attempted"], res["geocoded"], res["failed"])
    except Exception as e:
        log.error("geocode stage failed: %s", e, exc_info=True)

    # Снимок для графика на /admin/unbound — в конце цикла, когда все стадии
    # (rebind/complex_audit/complex_coords/geocode) уже отразились в базе.
    try:
        from bot.core.rebind import record_unbound_snapshot
        await record_unbound_snapshot()
    except Exception as e:
        log.error("unbound snapshot failed: %s", e, exc_info=True)

    log.info("=== Geobind cycle done ===")


async def fast_rebind_loop() -> None:
    from bot.core.rebind import record_unbound_snapshot, run_rebind
    while True:
        await asyncio.sleep(FAST_REBIND_INTERVAL_MIN * 60)
        try:
            res = await run_rebind(progress_cb=lambda s: log.info("fast-rebind: %s", s))
            log.info("fast-rebind: bound=%d (url=%d text=%d geo=%d) left=%d",
                      res["bound"], res["by_url"], res["by_text"], res["by_geo"], res["left"])
            await record_unbound_snapshot()
        except Exception as e:
            log.error("fast-rebind loop error: %s", e, exc_info=True)


async def main() -> None:
    from bot.db.pg import init_pool
    await init_pool(DATABASE_URL)

    once = "--once" in sys.argv

    if once:
        log.info("=== Geobind --once (единичный прогон) ===")
        await run_cycle()
        return

    log.info("=== Geobind service started ===")
    asyncio.create_task(fast_rebind_loop())
    while True:
        try:
            await run_cycle()
        except Exception as e:
            log.error("Geobind loop error: %s", e, exc_info=True)

        sleep_h = random.uniform(MIN_INTERVAL_H, MAX_INTERVAL_H)
        log.info("Sleeping %.1f hours...\n", sleep_h)
        await asyncio.sleep(sleep_h * 3600)


if __name__ == "__main__":
    asyncio.run(main())
