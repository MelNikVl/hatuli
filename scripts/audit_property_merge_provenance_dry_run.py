#!/usr/bin/env python3
"""scripts/audit_property_merge_provenance_dry_run.py — задача 2026-08-30,
"закрыть auditability/provenance gap перед следующими physical merges",
п.7 "REAL-DATA READ-ONLY CHECK". Read-only, ничего не пишет в БД (ни
manifest_log/execution_log/validation_log, НИ property_merge_provenance_
note — тот один allowed-to-write путь описан отдельно, см. --note-preview
ниже, и требует отдельного явного шага, не этого скрипта).

Два раздела:

1. `plan_property_merge()` dry-run по ВСЕМ текущим accepted-компонентам —
   ровно то же, что scripts/audit_property_merge_dry_run.py уже делает
   (эта функция engine не менялась в этом PR) — печатается кратко, полный
   отчёт уже есть в существующем скрипте, дублировать не нужно.

2. Демонстрация НОВОГО audit-формата (bot.identity.property_merge_
   provenance._run_validation_checks — та же чистая функция, которую
   validate_property_merge() использует для персистящихся проверок) на
   НЕСКОЛЬКИХ уже смерджженных (ДО этого PR, batch20/size3-canary,
   2026-08-20/21) production properties. Параметры для checks
   реконструируются ЗДЕСЬ, в Python, из property_merge_log (существующая
   таблица, единственный источник истины для прошлых repoint) — НЕ из
   manifest_log (там для этих merge-групп нет ни одной строки, и не
   должно быть, см. модульный докстринг property_merge_provenance.py и
   миграцию 093 про property_merge_provenance_note/is_reconstructed).
   Результат печатается, НЕ персистится — validate_property_merge()
   (персистящая версия) требует РЕАЛЬНЫЙ execution_id, которого для этих
   операций нет и не будет: писать в validation_log значило бы утверждать
   "эта проверка была выполнена в рамках учтённого apply", что было бы
   неправдой (задача, явно: "не пытаться дорисовать").
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")


async def _legacy_component_params(canonical_property_id: int) -> dict | None:
    """Реконструирует (losing_property_ids, expected_listing_ids,
    candidate_ids) для ОДНОЙ уже смерджженной (rolled_back_at IS NULL)
    merge-группы — читает ИСКЛЮЧИТЕЛЬНО property_merge_log, ничего не
    придумывает сверх того, что там реально записано."""
    from bot.db.pg import fetch

    rows = await fetch(
        "SELECT losing_property_id, moved_listing_ids, decision_source "
        "FROM property_merge_log WHERE canonical_property_id = $1 AND rolled_back_at IS NULL "
        "ORDER BY merge_id",
        canonical_property_id,
    )
    if not rows:
        return None

    losing_ids: list[int] = []
    expected_listing_ids: dict[str, list[str]] = {}
    candidate_ids: set[int] = set()
    for r in rows:
        lid = r["losing_property_id"]
        losing_ids.append(lid)
        moved = r["moved_listing_ids"]
        if isinstance(moved, str):
            moved = json.loads(moved)
        expected_listing_ids[str(lid)] = moved
        decision = r["decision_source"]
        if isinstance(decision, str):
            decision = json.loads(decision)
        candidate_ids.update(decision.get("candidate_ids", []))

    return {
        "canonical_property_id": canonical_property_id,
        "losing_property_ids": losing_ids,
        "expected_listing_ids": expected_listing_ids,
        "candidate_ids": sorted(candidate_ids),
    }


async def _demo_legacy_validation(canonical_property_ids: list[int]) -> None:
    from bot.identity.property_merge_provenance import _run_validation_checks

    print("\n" + "=" * 70)
    print("Раздел 2 — новый audit-формат (checks), применённый к УЖЕ")
    print("смердженным (ДО этого PR) properties. Read-only, НИЧЕГО не")
    print("персистится в validation_log (нет реального execution_id).")
    print("=" * 70)
    for cid in canonical_property_ids:
        params = await _legacy_component_params(cid)
        if params is None:
            print(f"\ncanonical={cid}: нет active property_merge_log строк — пропуск")
            continue
        checks = await _run_validation_checks(
            canonical_property_id=params["canonical_property_id"],
            losing_property_ids=params["losing_property_ids"],
            expected_listing_ids=params["expected_listing_ids"],
            candidate_ids=params["candidate_ids"],
        )
        passed = all(c["passed"] for c in checks)
        print(f"\ncanonical={cid}  losing={params['losing_property_ids']}  "
              f"[RECONSTRUCTED evidence, NOT originally persisted]  passed={passed}")
        for c in checks:
            mark = "OK" if c["passed"] else "FAIL"
            print(f"    [{mark}] {c['name']}: {c['detail']}")


async def _plan_summary() -> None:
    from bot.identity.property_merge import plan_property_merge

    print("=" * 70)
    print("Раздел 1 — plan_property_merge() dry-run, все текущие accepted-")
    print("компоненты (движок НЕ менялся в этом PR — полный отчёт см. в")
    print("scripts/audit_property_merge_dry_run.py). Здесь только сводка.")
    print("=" * 70)
    plans = await plan_property_merge()
    by_status: dict[str, int] = {}
    for p in plans:
        by_status[p["status"]] = by_status.get(p["status"], 0) + 1
    print(json.dumps(by_status, indent=2, ensure_ascii=False))


async def main() -> None:
    from bot.db.pg import close_pool, init_pool

    await init_pool(DATABASE_URL)
    try:
        await _plan_summary()
        # Пара примеров из batch20 (2026-08-20) + size3-canary reapply
        # (2026-08-21) — не все 22, показательная выборка размеров 1/2
        # losing.
        await _demo_legacy_validation([1971, 32446, 6127, 36269])
    finally:
        await close_pool()

    print("\n" + "=" * 70)
    print("НИЧЕГО не записано в БД — этот скрипт read-only от начала до конца.")
    print("Никакой property_merge_provenance_note строки этот скрипт не создаёт")
    print("(см. модульный докстринг — отдельный явный шаг, не автоматический).")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
