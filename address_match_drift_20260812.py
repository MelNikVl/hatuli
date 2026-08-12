#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Изолированный дрейф-отчёт: сколько пар меняют address_match()-вердикт
ИМЕННО из-за коммита 2ff574b (район/уч. — тоже шум), при прочих равных.
Сравнивает старый и новый шум-лист на реальных address-парах из
calibrate_homeportal_dry.py (60-объектная выборка) — не полная
пере-калибровка, а точечная атрибуция одного code change.
"""
import asyncio
import sys

sys.path.insert(0, ".")
from dotenv import load_dotenv
load_dotenv()
import os

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

_ADDR_NOISE_OLD = [
    "рк,", "г. астана", "г.астана", "астана г.", "астана,", "район ", "р-н ",
    "жилой массив", "ж/м", "мкр", "мкр.", "проспект", "пр.", "улица", "ул.",
    "переулок", "пер.",
]


def _normalize_address_old(addr: str) -> set[str]:
    s = (addr or "").lower()
    for token in _ADDR_NOISE_OLD:
        s = s.replace(token, " ")
    return {t for t in s.replace(",", " ").split() if len(t) > 1}


def address_match_old(addr_a, addr_b):
    if not addr_a or not addr_b:
        return None
    ta, tb = _normalize_address_old(addr_a), _normalize_address_old(addr_b)
    if not ta or not tb:
        return None
    overlap = len(ta & tb)
    return overlap / min(len(ta), len(tb)) >= 0.5


async def main():
    from bot.db.pg import init_pool, close_pool, fetch, fetchrow
    from bot.core.entity_resolution import address_match as address_match_new

    await init_pool(DATABASE_URL)
    rows = await fetch("""
        SELECT object_id, name, address, matched_complex_id FROM homeportal_objects
        WHERE matched_complex_id IS NOT NULL ORDER BY object_id ASC LIMIT 60
    """)
    flips_true_to_false, flips_false_to_true, both_true, both_false_or_none = 0, 0, 0, 0
    examined = 0
    for r in rows:
        cx = await fetchrow("SELECT address FROM complexes WHERE id = $1", r["matched_complex_id"])
        if not cx or not cx["address"] or not r["address"]:
            continue
        examined += 1
        old = address_match_old(cx["address"], r["address"])
        new = address_match_new(cx["address"], r["address"])
        if old and not new:
            flips_true_to_false += 1
            print(f"  TRUE->FALSE #{r['object_id']}: our={cx['address']!r} | hp={r['address']!r}")
        elif not old and new:
            flips_false_to_true += 1
            print(f"  FALSE->TRUE #{r['object_id']}: our={cx['address']!r} | hp={r['address']!r}")
        elif old and new:
            both_true += 1
        else:
            both_false_or_none += 1

    print(f"\nИТОГ: пар с обоими адресами={examined}, "
          f"было_true_стало_false={flips_true_to_false}, "
          f"было_false_стало_true={flips_false_to_true}, "
          f"оба_true={both_true}, оба_false={both_false_or_none}")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
