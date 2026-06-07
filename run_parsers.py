#!/usr/bin/env python3
"""Запуск парсеров без бота"""
import asyncio
from bot.db.pg import init_pool
from bot.core.apartment_parser import parse_all_apartments
from bot.core.rental_parser import parse_rental_listings

async def main():
    await init_pool('postgresql://krisha:123@localhost/krisha_bot')
    print("Parsing apartments...")
    await parse_all_apartments()
    print("Parsing rentals...")
    await parse_rental_listings()
    print("Done")

asyncio.run(main())
