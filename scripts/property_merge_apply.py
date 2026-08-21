#!/usr/bin/env python3
"""scripts/property_merge_apply.py — задача 2026-08-20, "Safe Physical
Property Merge", шаг 2 из 2. Принимает ТОЛЬКО один frozen manifest-файл
(из scripts/property_merge_plan.py) — НИКОГДА не строит собственный
"живой" список accepted-кандидатов (см. bot/identity/property_merge.py
докстринг про photo-canary live-reselecting query).

Дефолт — planning/dry-run mode (ничего не пишет, только проверяет и
печатает, что БЫ произошло). --apply обязателен для реальной записи.

    venv/bin/python scripts/property_merge_apply.py property_merge_manifests/property_merge_25757_....json
    venv/bin/python scripts/property_merge_apply.py --apply --actor "nik" property_merge_manifests/....json
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
    ap.add_argument("manifest_path")
    ap.add_argument("--apply", action="store_true", help="реально выполнить repoint (без флага — dry-run/planning)")
    ap.add_argument("--actor", default=os.getenv("USER", "unknown"), help="кто выполняет (в property_merge_log.executed_by)")
    args = ap.parse_args()

    from bot.db.pg import close_pool, init_pool
    from bot.identity.property_merge import apply_property_merge, load_manifest

    manifest = load_manifest(args.manifest_path)

    await init_pool(DATABASE_URL)
    try:
        result = await apply_property_merge(manifest, actor=args.actor, dry_run=not args.apply)
    finally:
        await close_pool()

    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))

    if result["status"] in ("blocked_stale", "blocked_conflict"):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
