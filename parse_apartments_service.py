#!/usr/bin/env python3
"""Отдельный парсер продаж"""
import asyncio
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from bot.db.pg import init_pool
from bot.core.apartment_parser import parse_apartments_for_sale

async def main():
    await init_pool('postgresql://krisha:123@localhost/krisha_bot')
    while True:
        print("Parsing apartments...")
        await parse_apartments_for_sale(city="astana", max_pages=10, max_price=200_000_000)
        print("Waiting 30 minutes...")
        await asyncio.sleep(30 * 60)

if __name__ == "__main__":
    asyncio.run(main())
