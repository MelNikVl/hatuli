"""
Debug Telegram bot - with detailed logging
"""
import asyncio
import logging
import os
import sys
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.utils import executor

# Configure detailed logging
logging.basicConfig(
    level=logging.DEBUG,  # DEBUG level for maximum info
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('bot_debug.log')
    ]
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# Get bot token from environment
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    logger.error("BOT_TOKEN not found in .env file")
    exit(1)

logger.info(f"Bot token: {BOT_TOKEN[:10]}...")

# Initialize bot and dispatcher
try:
    bot = Bot(token=BOT_TOKEN)
    logger.info("Bot object created")
    
    storage = MemoryStorage()
    logger.info("Storage created")
    
    dp = Dispatcher(bot, storage=storage)
    logger.info("Dispatcher created")
    
except Exception as e:
    logger.error(f"Failed to initialize bot: {e}")
    exit(1)


@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    """Handle /start command"""
    logger.info(f"User {message.from_user.id} sent /start")
    logger.debug(f"Message: {message}")
    
    try:
        response = await message.answer(
            "👋 Привет! Я бот для мониторинга недвижимости.\n\n"
            "Я буду присылать вам подходящие объявления с Krisha.kz.\n\n"
            "Используйте команды:\n"
            "/start - начать работу\n"
            "/help - помощь\n"
            "/status - статус\n\n"
            "Бот работает! 🎉"
        )
        logger.info(f"Response sent: {response}")
    except Exception as e:
        logger.error(f"Failed to send response: {e}")


@dp.message_handler(commands=['help'])
async def cmd_help(message: types.Message):
    """Handle /help command"""
    logger.info(f"User {message.from_user.id} sent /help")
    try:
        await message.answer(
            "📋 Доступные команды:\n\n"
            "/start - начать работу\n"
            "/help - эта справка\n"
            "/status - статус настроек\n\n"
            "Бот автоматически ищет объявления по вашим критериям."
        )
    except Exception as e:
        logger.error(f"Failed to send help: {e}")


@dp.message_handler(commands=['status'])
async def cmd_status(message: types.Message):
    """Handle /status command"""
    logger.info(f"User {message.from_user.id} sent /status")
    try:
        await message.answer(
            "✅ Бот работает!\n\n"
            "Статус: Активен\n"
            "Мониторинг: Включен\n"
            "Пользователь: " + str(message.from_user.id)
        )
    except Exception as e:
        logger.error(f"Failed to send status: {e}")


@dp.message_handler()
async def echo_message(message: types.Message):
    """Echo other messages"""
    logger.info(f"User {message.from_user.id} said: {message.text}")
    try:
        await message.answer(f"Вы сказали: {message.text}")
    except Exception as e:
        logger.error(f"Failed to echo: {e}")


if __name__ == '__main__':
    logger.info("=" * 50)
    logger.info("STARTING BOT IN DEBUG MODE")
    logger.info("=" * 50)
    
    try:
        executor.start_polling(dp, skip_updates=True)
    except Exception as e:
        logger.error(f"Bot crashed: {e}")
        import traceback
        traceback.print_exc()