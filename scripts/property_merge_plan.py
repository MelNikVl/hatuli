#!/usr/bin/env python3
"""scripts/property_merge_plan.py — задача 2026-08-20, "Safe Physical
Property Merge", шаг 1 из 2 двухшагового workflow (см. bot/identity/
property_merge.py докстринг). Read-only: строит connected components из
ЖИВЫХ accepted candidate-рёбер, ревалидирует, и печатает/сохраняет ОДИН
frozen manifest на КАЖДЫЙ 'planned' компонент. Ничего не пишет в БД —
единственный писатель во всём этом PR — scripts/property_merge_apply.py,
и только с --apply.

    venv/bin/python scripts/property_merge_plan.py --all --out-dir property_merge_manifests
    venv/bin/python scripts/property_merge_plan.py --property-id 25757 --out-dir property_merge_manifests
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
    ap.add_argument("--all", action="store_true", help="план по ВСЕМ текущим accepted-компонентам")
    ap.add_argument("--property-id", type=int, action="append", default=[],
                     help="план только по компоненту(ам), содержащим этот property_id (можно повторять)")
    ap.add_argument("--out-dir", default="property_merge_manifests",
                     help="куда сохранить manifest'ы 'planned' компонентов (создаётся, если нет)")
    ap.add_argument("--print-only", action="store_true", help="только печать, не сохранять файлы")
    args = ap.parse_args()

    if not args.all and not args.property_id:
        raise SystemExit("укажите --all или хотя бы один --property-id")

    from bot.db.pg import close_pool, init_pool
    from bot.identity.property_merge import plan_property_merge, save_manifest

    await init_pool(DATABASE_URL)
    try:
        target = None if args.all else set(args.property_id)
        plans = await plan_property_merge(target)
    finally:
        await close_pool()

    planned = [p for p in plans if p["status"] == "planned"]
    blocked = [p for p in plans if p["status"] == "blocked"]

    print(f"компонент всего: {len(plans)}  planned: {len(planned)}  blocked: {len(blocked)}")

    if not args.print_only and planned:
        os.makedirs(args.out_dir, exist_ok=True)

    for p in plans:
        if p["status"] == "planned":
            manifest = p["manifest"]
            print(f"  PLANNED  members={p['members']}  canonical={p['canonical_property_id']}  "
                  f"losing={p['losing_property_ids']}  component_hash={manifest['component_hash'][:12]}...")
            if p.get("warnings"):
                # Soft warnings (напр. floor_mismatch) — НЕ блокируют, но
                # заслуживают взгляда оператора перед --apply (см. bot/
                # identity/property_merge.py, "floor consistency audit").
                for w in p["warnings"]:
                    print(f"    WARNING  {w['reason']}: {w['detail']}")
            if not args.print_only:
                fname = f"property_merge_{manifest['canonical_property_id']}_{manifest['component_hash'][:12]}.json"
                path = os.path.join(args.out_dir, fname)
                save_manifest(manifest, path)
                print(f"    -> {path}")
        else:
            reasons = {r["reason"] for r in p["blocked_reasons"]}
            print(f"  BLOCKED  members={p['members']}  reasons={sorted(reasons)}")
            for r in p["blocked_reasons"]:
                print(f"    - {r.get('reason')}: {r.get('detail')}")


if __name__ == "__main__":
    asyncio.run(main())
