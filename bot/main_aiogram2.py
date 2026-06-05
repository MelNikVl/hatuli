"""
Bot entry point - COMPATIBLE VERSION for aiogram 2.x.

Sets up the aiogram dispatcher, registers routers, initialises the DB,
and starts polling.
"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage

from bot.config import load_config
from bot.db.models import init_db
from bot.handlers import alerts as alerts_handler
from bot.handlers import start as start_handler

logger = logging.getLogger(__name__)


def _make_middleware(db_path: str):
    """
    Middleware that injects `db_path` as a keyword argument into every handler.
    For aiogram 2.x we use a simple middleware.
    """
    from aiogram import Dispatcher
    from aiogram.types import Update
    
    async def db_middleware(handler, event: Update, data: dict):
        data["db_path"] = db_path
        return await handler(event, data)
    
    return db_middleware


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg = load_config()

    # Initialise DB
    await init_db(cfg.db_path)
    logger.info("Database initialised at %s", cfg.db_path)

    # Aiogram 2.x setup
    bot = Bot(token=cfg.bot_token, parse_mode=types.ParseMode.HTML)
    storage = MemoryStorage()
    dp = Dispatcher(bot, storage=storage)

    # Register middleware
    db_mw = _make_middleware(cfg.db_path)
    dp.middleware.setup(db_mw)

    # Register handlers (aiogram 2.x style)
    dp.register_message_handler(start_handler.start_command, commands=["start"])
    dp.register_message_handler(alerts_handler.alerts_command, commands=["alerts"])
    dp.register_callback_query_handler(alerts_handler.alerts_callback)

    logger.info("Starting bot polling…")
    try:
        await dp.start_polling()
    finally:
        await dp.storage.close()
        await dp.storage.wait_closed()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())