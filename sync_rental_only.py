#!/usr/bin/env python3
"""
Скрипт для синхронизации rental_listings в Google Sheets.
Запускать по расписанию (cron) или вручную.
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from bot.db.pg import init_pool
from bot.core.sheets_sync_rental import sync_rental_to_sheets

async def main():
    print("Connecting to PostgreSQL...")
    await init_pool('postgresql://krisha:123@localhost/krisha_bot')
    print("Syncing rental listings to Google Sheets...")
    await sync_rental_to_sheets()
    print("Done!")

if __name__ == "__main__":
    asyncio.run(main())
