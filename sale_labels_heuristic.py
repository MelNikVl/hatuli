#!/usr/bin/env python3
"""Ежедневные авто-метки «перевыставлено» для ушедших объявлений (bot/core/sale_labels.py)."""
import logging
import os
import sys

import psycopg2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bot.core.sale_labels import find_relists, upsert_heuristic_relists  # noqa: E402

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))
    conn = psycopg2.connect(os.environ['DATABASE_URL'])
    pairs = find_relists(conn)
    n = upsert_heuristic_relists(conn, pairs)
    logging.info('relist-кандидатов %d, записано/обновлено %d', len(pairs), n)
