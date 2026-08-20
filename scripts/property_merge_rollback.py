#!/usr/bin/env python3
"""scripts/property_merge_rollback.py — задача 2026-08-20, "Safe Physical
Property Merge". Откат ОДНОЙ merge-операции по её merge_group_key
(печатается в результате scripts/property_merge_apply.py --apply).

    venv/bin/python scripts/property_merge_rollback.py <merge_group_key> --reason "..." --actor nik
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("merge_group_key")
    ap.add_argument("--reason", required=True)
    ap.add_argument("--actor", default=os.getenv("USER", "unknown"))
    args = ap.parse_args()

    from bot.db.pg import close_pool, init_pool
    from bot.identity.property_merge import rollback_property_merge

    await init_pool(DATABASE_URL)
    try:
        result = await rollback_property_merge(args.merge_group_key, actor=args.actor, reason=args.reason)
    finally:
        await close_pool()

    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    if result["status"] != "rolled_back":
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
