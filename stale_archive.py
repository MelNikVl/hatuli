#!/usr/bin/env python3
"""Ежедневная архивация объявлений, пропавших из выдачи (bot/core/stale_archive.py).

  venv/bin/python stale_archive.py --dry-run   # только посчитать
  venv/bin/python stale_archive.py
"""
import argparse
import asyncio
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


async def main(dry_run: bool) -> int:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))
    from bot.db.pg import init_pool, close_pool
    from bot.core.stale_archive import archive_stale, archive_stale_rentals, reactivate_seen
    await init_pool(os.environ['DATABASE_URL'])
    try:
        reactivated = await reactivate_seen(dry_run=dry_run)
        res = await archive_stale(dry_run=dry_run)
        res['reactivated'] = reactivated
        rent = await archive_stale_rentals(dry_run=dry_run)
    finally:
        await close_pool()
    print(json.dumps({'sale': res, 'rent': rent}, ensure_ascii=False))
    return 0 if res['healthy'] else 3


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    sys.exit(asyncio.run(main(a.dry_run)))
