#!/usr/bin/env python3
"""Запуск парсинга без бота (аренда + продажа)"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from bot.db.pg import init_pool
from bot.core.apartment_parser import parse_apartments_for_sale
from bot.core.rental_parser import run_rental_cycle

async def main():
    await init_pool('postgresql://krisha:123@localhost/krisha_bot')
    
    print("=== Apartment parser ===")
    await parse_apartments_for_sale(city="astana", max_pages=5, max_price=200_000_000)
    
    print("=== Rental parser ===")
    await run_rental_cycle()
    
    print("Done")

if __name__ == "__main__":
    asyncio.run(main())
