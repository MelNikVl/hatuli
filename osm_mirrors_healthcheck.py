#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Healthcheck зеркал Overpass (Фаза L1 продуктового трека «Локация»,
docs/location_product_design.md §7, задача 2026-08-14) — п.9 "Часть 3"
scoring_roadmap.md: "healthcheck зеркал Overpass с алертом при <2 живых
(сейчас, по докстрингу bot/score_layers/osm.py, часто жив только 1 из 4)".

НЕ участвует в обычном пути слоёв локации (bot/score_layers/osm.py::
overpass_request()/overpass_cached() не меняются) — отдельный лёгкий
пинг всех 4 зеркал независимо (bot/score_layers/osm.py::check_mirrors()),
не гео-запрос, не грузит зеркала реальными данными.

Алертит через bot/core/admin_alert.py::notify_admin() (тот же Telegram-
канал, что уже использует ai_text_analysis.py — общий модуль, не вторая
копия) только если живых <2 зеркал — не на каждый минус одно зеркало
(overpass_request() и так переживает единичные отказы перебором).

Расписание: krisha-osm-healthcheck.timer (ежедневно).
Разовая проверка: venv/bin/python osm_mirrors_healthcheck.py
"""
from __future__ import annotations

import asyncio
import logging

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.FileHandler("osm_mirrors_healthcheck.log", encoding="utf-8", errors="replace")],
)
log = logging.getLogger("osm_mirrors_healthcheck")

ALIVE_THRESHOLD = 2  # алерт, если живых зеркал СТРОГО МЕНЬШЕ этого числа


async def run_healthcheck() -> dict:
    from bot.score_layers.osm import check_mirrors, OVERPASS_MIRRORS
    from bot.core.admin_alert import notify_admin

    results = await check_mirrors()
    alive = [m for m, ok in results.items() if ok]
    dead = [m for m, ok in results.items() if not ok]
    log.info("Overpass mirrors: %d/%d живы. Живые: %s. Мёртвые: %s",
              len(alive), len(OVERPASS_MIRRORS), alive, dead)

    if len(alive) < ALIVE_THRESHOLD:
        text = (
            f"⚠️ Overpass: только {len(alive)}/{len(OVERPASS_MIRRORS)} зеркал живы "
            f"(порог {ALIVE_THRESHOLD}).\nЖивые: {alive or '—'}\nМёртвые: {dead}"
        )
        await notify_admin(text)
        log.warning("Алерт отправлен: %s", text)

    return {"alive": alive, "dead": dead, "total": len(OVERPASS_MIRRORS)}


async def main() -> None:
    # Без init_pool() намеренно — check_mirrors()/notify_admin() не трогают
    # Postgres вовсе, в отличие от остальных скриптов этого класса.
    await run_healthcheck()


if __name__ == "__main__":
    asyncio.run(main())
