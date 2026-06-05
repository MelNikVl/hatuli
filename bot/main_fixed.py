"""
Bot entry point - FIXED VERSION for aiogram 3.x.

Sets up the aiogram dispatcher, registers routers, initialises the DB,
and starts polling.
"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from bot.config import load_config
from bot.db.models import init_db
from bot.handlers import alerts as alerts_handler
from bot.handlers import start as start_handler

logger = logging.getLogger(__name__)


def _make_middleware(db_path: str):
    """
    Middleware that injects `db_path` as a keyword argument into every handler.
    Aiogram 3 supports this via the `data` dict passed to filters/handlers.
    """
    from aiogram import BaseMiddleware
    from aiogram.types import TelegramObject

    class DbMiddleware(BaseMiddleware):
        async def __call__(self, handler, event: TelegramObject, data: dict):
            data["db_path"] = db_path
            return await handler(event, data)

    return DbMiddleware()


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg = load_config()

    # Initialise DB
    await init_db(cfg.db_path)
    logger.info("Database initialised at %s", cfg.db_path)

    # FIXED: Remove DefaultBotProperties for aiogram 3.x
    bot = Bot(
        token=cfg.bot_token,
        parse_mode=ParseMode.HTML,
    )

    dp = Dispatcher(storage=MemoryStorage())

    # Inject db_path into every update handler
    db_mw = _make_middleware(cfg.db_path)
    dp.message.middleware(db_mw)
    dp.callback_query.middleware(db_mw)

    # Register routers
    dp.include_router(start_handler.router)
    dp.include_router(alerts_handler.router)

    logger.info("Starting bot polling…")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())