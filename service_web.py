#!/usr/bin/env python3
"""
Веб-терминал аналитики.
FastAPI + Jinja2, порт 8082.

Запуск:  python service_web.py
URL:     http://0.0.0.0:8082/admin/analytics
"""
import asyncio
import logging
import os

import uvicorn
from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
DB_PATH = os.getenv("DB_PATH", "bot.db")


async def main():
    from bot.db.pg import init_pool
    from bot.db.compat import BotDB
    from bot.admin_web import create_admin_app

    await init_pool(DATABASE_URL)

    db = BotDB(DB_PATH)
    await db.init()

    app = create_admin_app(db, ADMIN_PASSWORD, "2.0", DB_PATH)

    config = uvicorn.Config(app=app, host="0.0.0.0", port=8082, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
