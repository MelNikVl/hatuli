#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проверка audit_complexes после патча."""
import asyncio, sys
sys.path.insert(0, '/home/nik/krisha_bot')
from bot.db.pg import init_pool, close_pool
from bot.core.complex_audit import audit_complexes

async def main():
    await init_pool('postgresql://krisha@localhost/krisha_bot')
    try:
        res = await audit_complexes()
        print('подозрительных:', len(res))
        for r in res[:15]:
            print(' ', r['id'], r['name'][:30], f"{r['share']:.0%}", r['reason'])
    finally:
        await close_pool()

asyncio.run(main())
