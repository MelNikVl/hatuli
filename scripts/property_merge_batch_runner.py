#!/usr/bin/env python3
"""scripts/property_merge_batch_runner.py — controlled batch runner
(задача: "SAFE BATCH RUNNER... load frozen manifest -> apply -> assert
status == applied -> validate -> assert validation == passed -> только
тогда переходить к следующему. При любом blocked/stale/conflict/exception/
validation failure — немедленный STOP. Никакого continue.").

Принимает ТОЛЬКО заранее подготовленный список manifest-путей (JSON-файл,
--batch-spec) — НИКОГДА не строит список сам ("возьми следующие N accepted
компонент" здесь не существует, задача явно это запрещает). Формат
batch-spec:

    [
      {"manifest_path": "property_merge_manifests/property_merge_1971_....json",
       "expected_component_hash": "6a3cb99..."},
      ...
    ]

`expected_component_hash` опционален, но настоятельно рекомендован —
защита от "не тот файл попал в batch-spec" ДО того, как manifest вообще
попадёт в persist_manifest()/apply. Дефолт — dry-run ВСЕГО батча (ничего
не пишет, кроме manifest-персиста + execution-лога с dry_run=true);
--apply обязателен для реального repoint.

    venv/bin/python scripts/property_merge_batch_runner.py batch.json
    venv/bin/python scripts/property_merge_batch_runner.py --apply --actor nik batch.json
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


def _load_batch_spec(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        items = json.load(f)
    if not isinstance(items, list) or not items:
        raise ValueError("batch-spec must be a non-empty JSON list")
    for i, item in enumerate(items):
        if not isinstance(item, dict) or "manifest_path" not in item:
            raise ValueError(f"batch-spec[{i}] must be an object with 'manifest_path'")
    return items


async def _run(items: list[dict], *, actor: str, apply: bool, allow_non_master: bool,
                override_reason: str | None, git_provenance: dict | None = None) -> int:
    from bot.identity.property_merge import load_manifest
    from bot.identity.property_merge_provenance import apply_property_merge_durable, validate_property_merge

    total = len(items)
    for i, item in enumerate(items, start=1):
        path = item["manifest_path"]
        expected_hash = item.get("expected_component_hash")
        print(f"\n[{i}/{total}] load {path}")

        try:
            manifest = load_manifest(path)
        except Exception as exc:
            print(f"  STOP: cannot load manifest — {type(exc).__name__}: {exc}")
            return 1

        if expected_hash and manifest["component_hash"] != expected_hash:
            print(f"  STOP: component_hash mismatch — batch-spec expected {expected_hash!r}, "
                  f"file has {manifest['component_hash']!r}. File on disk changed since batch-spec "
                  f"was prepared — re-plan required, refusing to persist/apply.")
            return 1

        result = await apply_property_merge_durable(
            manifest, actor=actor, dry_run=not apply, git_provenance=git_provenance,
            allow_non_master=allow_non_master, override_reason=override_reason,
        )
        status = result["status"]
        print(f"  apply -> status={status} execution_id={result.get('execution_id')}")
        if status not in ("merged", "already_merged", "would_apply"):
            print(f"  STOP: apply did not succeed (status={status!r}). "
                  f"Remaining {total - i} component(s) NOT attempted.")
            print(json.dumps(result, indent=2, default=str, ensure_ascii=False))
            return 1

        if apply and status in ("merged", "already_merged"):
            validation = await validate_property_merge(result["execution_id"])
            print(f"  validate -> passed={validation['passed']}")
            if not validation["passed"]:
                print(f"  STOP: validation failed. Remaining {total - i} component(s) NOT attempted. "
                      f"No auto-rollback performed — inspect and decide manually.")
                print(json.dumps(validation["checks"], indent=2, default=str, ensure_ascii=False))
                return 1

    print(f"\n{total}/{total} components completed cleanly.")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("batch_spec", help="путь к JSON-файлу со списком {'manifest_path', 'expected_component_hash'?}")
    ap.add_argument("--apply", action="store_true", help="реально выполнить batch (без флага — dry-run всего батча)")
    ap.add_argument("--actor", default=os.getenv("USER", "unknown"))
    ap.add_argument("--allow-non-master", action="store_true",
                     help="явный override guard'а 'apply только с master' — попадает в audit log")
    ap.add_argument("--override-reason", default=None,
                     help="обязателен вместе с --allow-non-master (иначе guard всё равно блокирует)")
    args = ap.parse_args()

    if args.allow_non_master and not args.override_reason:
        ap.error("--allow-non-master requires --override-reason (must be recorded in the audit log)")

    items = _load_batch_spec(args.batch_spec)

    async def _main() -> int:
        from bot.db.pg import close_pool, init_pool
        await init_pool(DATABASE_URL)
        try:
            return await _run(items, actor=args.actor, apply=args.apply,
                               allow_non_master=args.allow_non_master, override_reason=args.override_reason)
        finally:
            await close_pool()

    exit_code = asyncio.run(_main())
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
