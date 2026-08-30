#!/usr/bin/env python3
"""scripts/property_merge_backfill_legacy_provenance_note.py — задача
2026-08-30, п.7: "Можно сделать отдельную запись вида legacy_provenance_
incomplete, но чётко отделить reconstructed evidence от originally
persisted evidence."

НЕ запускается автоматически этим PR (ветка/PR только добавляет engine +
tests + read-only demo — задача явно: "не пытаться дорисовать"). Этот
скрипт — отдельный, explicit, one-time инструмент: для каждого active
(rolled_back_at IS NULL) `merge_group_key` в property_merge_log, у
которого ЕЩЁ нет ни одной `property_merge_provenance_note` строки,
вставляет ОДНУ note (`note_type='legacy_provenance_incomplete'`,
`is_reconstructed=TRUE` — единственное значение, которое когда-либо
пишет этот скрипт) с детальным reconstructed evidence (git SHA bracketing
через reflog, отсутствие manifest-файлов на диске, интервалы между
executed_at, что конкретно НЕ удалось подтвердить) — то же содержание,
что read-only provenance audit (2026-08-30, чат) уже установил, теперь
как постоянная, явно помеченная as-reconstructed запись в БД, а не
только текст в истории чата.

Дефолт — dry-run (печатает, что БЫ вставилось, ничего не пишет).
--apply обязателен для реальной записи. Идемпотентен: merge_group_key,
для которого note УЖЕ есть, пропускается молча (см. --force для
исключения, ожидается НЕ использоваться — append-only, повторная нота
на тот же group_key создаёт вторую строку, а не заменяет первую).

    venv/bin/python scripts/property_merge_backfill_legacy_provenance_note.py
    venv/bin/python scripts/property_merge_backfill_legacy_provenance_note.py --apply --actor nik
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

# Read-only provenance audit findings (2026-08-30, эта задача) — тот же
# факт-набор для ВСЕХ legacy-групп (batch20 + size3), в этом файле не
# пересчитывается заново из живых данных: это ЗАСТЫВШИЙ снимок того, что
# аудит смог и не смог подтвердить в этот конкретный момент, намеренно
# (задача, явно: reconstructed evidence, не переисполняемая проверка).
_RECONSTRUCTED_EVIDENCE = {
    "audit_performed_at": "2026-08-30 (см. чат-сессию, тот же день, что это добавление)",
    "confirmed": [
        "property_merge_log: 24 строки (20 batch20 + 4 size3 rollback/reapply), "
        "все canonical/losing пары совпадают 1:1 с исходным списком 20 candidate_id",
        "git reflog: batch20 (2026-08-20 16:41:44) выполнен при HEAD=master@75c9e50 "
        "(bracketing checkout-истории 16:39:48 -> 09:47:24 следующего дня, без checkout между)",
        "git reflog: size3-canary (2026-08-21 09:54:44) выполнен при HEAD=feat/rebrand-clearly@"
        "bc288af (НЕ master; коммит bc288af трогает только user-facing брендинг, "
        "0 пересечения с bot/identity/property_merge.py/migrations/)",
        "current state: 22/22 losing properties identity_status='merged', 0 property_listings "
        "каждая, canonical listings корректны, timelines (build_property_timeline) читаются "
        "без ошибок, никаких новых conflict_reasons/stale evidence внутри этих компонент",
    ],
    "could_not_confirm": [
        "ни одного frozen manifest JSON-файла на диске (ожидаемый путь "
        "property_merge_manifests/property_merge_<canonical>_<hash12>.json) — не найдено нигде "
        "на файловой системе",
        "ни одного execution-лога/audit-следа реального apply-вызова (эта задача добавляет "
        "такой журнал ВПЕРВЫЕ, migrations/093)",
        "ни одного зафиксированного вызова Property Timeline-валидации МЕЖДУ операциями batch20 "
        "(интервалы 8-19мс между 20 apply механически совместимы только с одним in-process "
        "loop, не с 20 отдельными CLI-запусками property_merge_apply.py с их init_pool/close_pool "
        "overhead)",
        "не может быть доказано, что loop содержал per-pair fail-stop (batch20 не встретил ни "
        "одного blocked/error исхода, чтобы это проверить эмпирически)",
    ],
}


async def _groups_without_note() -> list[dict]:
    from bot.db.pg import fetch

    rows = await fetch("""
        SELECT DISTINCT pml.merge_group_key, pml.canonical_property_id, pml.executed_by,
               min(pml.executed_at) AS first_executed_at, count(*) AS n_losing
        FROM property_merge_log pml
        WHERE pml.rolled_back_at IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM property_merge_provenance_note n WHERE n.merge_group_key = pml.merge_group_key
          )
        GROUP BY pml.merge_group_key, pml.canonical_property_id, pml.executed_by
        ORDER BY first_executed_at
    """)
    return [dict(r) for r in rows]


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="реально вставить notes (без флага — dry-run)")
    ap.add_argument("--actor", default=os.getenv("USER", "unknown"))
    args = ap.parse_args()

    from bot.db.pg import close_pool, execute, init_pool

    await init_pool(DATABASE_URL)
    try:
        groups = await _groups_without_note()
        print(f"{len(groups)} active merge_group_key(s) without a provenance_note yet:")
        for g in groups:
            print(f"  {g['merge_group_key']}  canonical={g['canonical_property_id']}  "
                  f"executed_by={g['executed_by']!r}  n_losing={g['n_losing']}  "
                  f"first_executed_at={g['first_executed_at']}")

        if not args.apply:
            print("\nDRY-RUN — ничего не записано. Повторить с --apply для реальной записи.")
            return

        for g in groups:
            detail = {
                **_RECONSTRUCTED_EVIDENCE,
                "merge_group_key": str(g["merge_group_key"]),
                "canonical_property_id": g["canonical_property_id"],
                "executed_by": g["executed_by"],
                "backfilled_at": datetime.now(timezone.utc).isoformat(),
            }
            await execute(
                """
                INSERT INTO property_merge_provenance_note
                    (merge_group_key, note_type, detail, is_reconstructed, created_by)
                VALUES ($1, 'legacy_provenance_incomplete', $2::jsonb, TRUE, $3)
                """,
                g["merge_group_key"], json.dumps(detail, ensure_ascii=False, default=str), args.actor,
            )
        print(f"\n{len(groups)} note(s) inserted (is_reconstructed=TRUE).")
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
