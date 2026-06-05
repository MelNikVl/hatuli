"""
Simple Telegram bot for testing - just responds to /start
"""
import asyncio
import logging
import os
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Get bot token from environment
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    logger.error("BOT_TOKEN not found in .env file")
    exit(1)

# Initialize bot and dispatcher
bot = Bot(token=BOT_TOKEN, parse_mode=types.ParseMode.HTML)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)


@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    """Handle /start command"""
    logger.info(f"User {message.from_user.id} sent /start")
    await message.answer(
        "👋 Привет! Я бот для мониторинга недвижимости.\n\n"
        "Я буду присылать вам подходящие объявления с Krisha.kz.\n\n"
        "Используйте команды:\n"
        "/start - начать работу\n"
        "/help - помощь\n"
        "/status - статус\n\n"
        "Бот работает! 🎉"
    )


@dp.message_handler(commands=['help'])
async def cmd_help(message: types.Message):
    """Handle /help command"""
    await message.answer(
        "📋 <b>Доступные команды:</b>\n\n"
        "/start - начать работу\n"
        "/help - эта справка\n"
        "/status - статус настроек\n\n"
        "Бот автоматически ищет объявления по вашим критериям."
    )


@dp.message_handler(commands=['status'])
async def cmd_status(message: types.Message):
    """Handle /status command"""
    await message.answer(
        "✅ <b>Бот работает!</b>\n\n"
        "Статус: Активен\n"
        "Мониторинг: Включен\n"
        "Пользователь: " + str(message.from_user.id)
    )


@dp.message_handler()
async def echo_message(message: types.Message):
    """Echo other messages"""
    await message.answer(f"Вы сказали: {message.text}")


async def main():
    """Main function to start the bot"""
    logger.info("Starting bot...")
    try:
        await dp.start_polling()
    finally:
        await dp.storage.close()
        await dp.storage.wait_closed()
        await bot.session.close()


if __name__ == '__main__':
    asyncio.run(main())