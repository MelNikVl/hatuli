#!/usr/bin/env python3
import asyncio
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from bot.db.pg import init_pool
from bot.core.rental_parser import run_rental_cycle

async def main():
    await init_pool('postgresql://krisha:123@localhost/krisha_bot')
    await run_rental_cycle()

if __name__ == "__main__":
    asyncio.run(main())
