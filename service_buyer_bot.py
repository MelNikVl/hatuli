"""Optional public buyer experiment. Separate token; no legacy SQLite imports."""
import asyncio
import logging
import os

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand, MenuButtonCommands
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from bot.buyer.telegram import router
from bot.db.pg import init_pool, close_pool


async def main() -> None:
    load_dotenv()
    token, dsn = os.getenv('BUYER_BOT_TOKEN'), os.getenv('DATABASE_URL')
    if not token or not dsn:
        raise SystemExit('Задайте BUYER_BOT_TOKEN и DATABASE_URL в .env')
    if token in (os.getenv('SITE_BOT_TOKEN'), os.getenv('BOT_TOKEN')):
        raise SystemExit('BUYER_BOT_TOKEN должен принадлежать отдельному боту')
    await init_pool(dsn)
    dp = Dispatcher(storage=MemoryStorage(), events_isolation=SimpleEventIsolation())
    dp.include_router(router)
    try:
        async with Bot(token) as bot:
            await bot.set_my_commands([BotCommand(command=c,description=d) for c,d in [
                ('menu','Главное меню'),('check','Проверить квартиру по ссылке'),
                ('nearby','Варианты рядом на карте'),('profile','Настроить подбор'),
                ('map','Выбрать места, где хочу жить'),('favorites','Сохранённые квартиры'),
                ('cancel','Отменить настройку')]])
            await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
            await dp.start_polling(bot)
    finally:
        await close_pool()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
