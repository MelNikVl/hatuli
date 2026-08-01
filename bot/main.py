"""
Bot entry point.

Sets up the aiogram dispatcher, registers routers, initialises the DB,
runs the admin web panel (FastAPI on :8080), and launches the random-interval
parser loop and APScheduler-based subscription/daily-report jobs.
"""
from __future__ import annotations

import asyncio
import logging

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from bot.admin_web import create_admin_app
from bot.config import load_config
from bot.db.compat import BotDB
from bot.db.models import init_db
from bot.handlers import alerts as alerts_handler
from bot.handlers import location as location_handler
from bot.handlers import menu as menu_handler
from bot.handlers import start as start_handler
from bot.jobs.scheduler import check_expired_subscriptions, check_price_changes, parser_loop, send_daily_reports, investment_loop, apartment_loop
from bot.db.pg import init_pool as pg_init_pool
from bot.core.rental_parser import run_rental_cycle

logger = logging.getLogger(__name__)


def _make_db_middleware(db_path: str):
    """Inject db_path into every handler via the data dict."""
    from aiogram import BaseMiddleware
    from aiogram.types import TelegramObject

    class DbMiddleware(BaseMiddleware):
        async def __call__(self, handler, event: TelegramObject, data: dict):
            data["db_path"] = db_path
            return await handler(event, data)

    return DbMiddleware()


def _make_request_counter_middleware(compat_db: BotDB):
    """Middleware that records every incoming update in bot_requests table."""
    from aiogram import BaseMiddleware
    from aiogram.types import TelegramObject

    class RequestCounterMiddleware(BaseMiddleware):
        async def __call__(self, handler, event: TelegramObject, data: dict):
            user = data.get("event_from_user")
            user_id = user.id if user else None
            try:
                await compat_db.log_bot_request(user_id)
            except Exception:
                pass  # never crash the bot over metrics
            return await handler(event, data)

    return RequestCounterMiddleware()


async def _run_admin_web(compat_db: BotDB, admin_password: str, bot_version: str, db_path: str) -> None:
    app = create_admin_app(compat_db, admin_password, bot_version, db_path=db_path)
    config = uvicorn.Config(app=app, host="0.0.0.0", port=8082, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


async def _rental_loop() -> None:
    """Rental index: one page every 5-15 min, rebuild index after each full pass."""
    import asyncio, random, logging
    from bot.core.rental_parser import (
        parse_rental_path, save_rental_listings,
        rebuild_rental_index, RENTAL_PATHS
    )
    log = logging.getLogger("rental_loop")
    paths = list(RENTAL_PATHS.items())  # [(path, prop_type), ...]
    path_idx = 0
    page_num = 1
    MAX_PAGES = 5  # страниц на один тип за цикл

    while True:
        try:
            path, prop_type = paths[path_idx]
            log.info("Rental: %s page %d", prop_type, page_num)
            listings = await parse_rental_path(path, prop_type, max_pages=1)
            # parse_rental_path с max_pages=1 даёт одну страницу,
            # но нам нужна конкретная страница — используем напрямую
            from bot.core.rental_parser import _fetch_page
            import httpx
            from bot.core.rental_parser import BASE_URL, DEFAULT_HEADERS
            url = BASE_URL + path + (f"?page={page_num}" if page_num > 1 else "")
            async with httpx.AsyncClient(follow_redirects=True) as client:
                listings = await _fetch_page(client, url, prop_type)
            if listings:
                saved = await save_rental_listings(listings)
                log.info("Rental: saved %d from %s page %d", saved, prop_type, page_num)
            # Следующая страница или следующий тип
            page_num += 1
            if page_num > MAX_PAGES or not listings:
                path_idx = (path_idx + 1) % len(paths)
                page_num = 1
                if path_idx == 0:
                    # Прошли все типы — пересчитываем индекс
                    log.info("Rental: rebuilding index...")
                    await rebuild_rental_index()
                    try:
                        from bot.core.sheets_sync_rental import sync_rental_to_sheets
                        await sync_rental_to_sheets()
                    except Exception as _e:
                        log.warning("rental sheets sync failed: %s", _e)
        except Exception as e:
            logging.getLogger("rental_loop").error("Rental error: %s", e, exc_info=True)

        await asyncio.sleep(random.uniform(5 * 60, 15 * 60))


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg = load_config()

    # Initialise PostgreSQL
    db_url = __import__('os').getenv('DATABASE_URL', '')
    if db_url:
        await pg_init_pool(db_url)
    # Initialise aiogram DB tables (SQLite compat layer)
    await init_db(cfg.db_path)

    # Initialise compat DB (subscription / scheduler / admin tables)
    compat_db = BotDB(cfg.db_path)
    await compat_db.init()

    logger.info("Database initialised at %s", cfg.db_path)

    bot = Bot(
        token=cfg.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    dp = Dispatcher(storage=MemoryStorage())

    # Middleware: inject db_path + count requests (all relevant update types)
    db_mw = _make_db_middleware(cfg.db_path)
    counter_mw = _make_request_counter_middleware(compat_db)
    for obs in (dp.message, dp.callback_query, dp.edited_message):
        obs.middleware(db_mw)
        obs.outer_middleware(counter_mw)

    # Register routers
    dp.include_router(start_handler.router)
    dp.include_router(menu_handler.router)
    dp.include_router(alerts_handler.router)
    dp.include_router(location_handler.router)

    # APScheduler: subscription expiry + daily reports (every 10 min)
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        check_expired_subscriptions, "interval", minutes=10,
        kwargs={"bot": bot, "db": compat_db},
    )
    scheduler.add_job(
        send_daily_reports, "interval", minutes=10,
        kwargs={"bot": bot, "db": compat_db},
    )
    scheduler.add_job(
        check_price_changes, "interval", minutes=30,
        kwargs={"bot": bot, "db_path": cfg.db_path},
    )

    async def _sync_rental_sheets():
        try:
            from bot.core.sheets_sync_rental import sync_rental_to_sheets
            await sync_rental_to_sheets()
        except Exception as e:
            logger.warning("rental sheets sync: %s", e)

    scheduler.add_job(_sync_rental_sheets, "interval", minutes=30)
    scheduler.start()

    logger.info("Starting bot polling…")

    await asyncio.gather(
        dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types()),
        _run_admin_web(compat_db, cfg.admin_password, cfg.bot_version, cfg.db_path),
        # parser_loop(bot, compat_db, cfg),
        _rental_loop(),
        investment_loop(bot, compat_db, cfg),
        apartment_loop(bot, compat_db, cfg),
    )


if __name__ == "__main__":
    import signal
    def _sigterm(*_):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, _sigterm)
    asyncio.run(main())
