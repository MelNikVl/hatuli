#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/import_reviewed_complex_relations.py — задача 2026-08-31,
"Complex Identity: human labeling + impact assessment", шаг 3:
validate-only import для файла, экспортированного review-инструментом
(complex_identity_review artifact — "Экспортировать JSON").

НИКАКИХ writes в БД — ни INSERT, ни UPDATE (задача, явно: "Никаких
automatic writes в complex_relations до отдельного approval"). Этот
скрипт только ЧИТАЕТ (проверка существования complex_id/дубликатов в
самой БД complex_relations, если таблица уже применена) и печатает/
пишет offline preview — готовые к ручной проверке INSERT-statements,
которые применяет человек (psql/миграция), не этот скрипт.

Вход: JSON, экспортированный review-инструментом, форма
{relations_for_import: [...], ambiguous_reviewed: [...], unreviewed_pairs: [...]}.

Проверки на каждую строку relations_for_import:
  - canonical order complex_id_a < complex_id_b (constraint migrations/095)
  - relation_type — один из 5 допустимых (БЕЗ 'ambiguous' — та же логика,
    что CHECK в migrations/095; если 'ambiguous' сюда просочился —
    ошибка данных на стороне review-инструмента, не пропускать молча)
  - confidence в [0, 1]
  - reviewed_by непустой, reviewed_at — валидный ISO timestamp
  - evidence — непустой dict (JSONB NOT NULL в схеме)
  - оба complex_id существуют в complexes (SELECT, read-only)
  - нет дублей пары внутри самого импортируемого файла (UNIQUE(a,b))
  - если complex_relations уже применена в этой БД — предупреждение,
    если пара УЖЕ там есть (не ошибка — дальше это UPDATE, не INSERT,
    задача явно: "исправление — UPDATE существующей строки, не второй
    INSERT"), решает человек, не скрипт

Выход:
  - печатает построчный отчёт (OK / ERROR / WARNING)
  - complex_relations_import_preview.sql — INSERT ... (и, для пар уже
    существующих в таблице, UPDATE ...) с комментарием "-- NOT EXECUTED,
    review before applying manually" в шапке
  - complex_relations_import_validation_report.json — машиночитаемый
    summary (n_ok/n_error/errors)

    venv/bin/python scripts/import_reviewed_complex_relations.py <exported.json>
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

_ALLOWED_RELATION_TYPES = {
    "duplicate_same_complex", "sibling_phase", "same_umbrella_project",
    "renamed_same_complex", "separate_neighbor_complex",
}


def _sql_str(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


async def main() -> None:
    if len(sys.argv) < 2:
        print("usage: venv/bin/python scripts/import_reviewed_complex_relations.py <exported.json>")
        sys.exit(1)
    in_path = sys.argv[1]
    with open(in_path, encoding="utf-8") as f:
        payload = json.load(f)

    from bot.db.pg import close_pool, fetch, init_pool
    await init_pool(DATABASE_URL)
    try:
        await run(payload, fetch)
    finally:
        await close_pool()


async def run(payload: dict, fetch) -> None:
    rows = payload.get("relations_for_import", [])
    print(f"validating {len(rows)} relations_for_import rows from {payload.get('generated_from')} "
          f"(exported_at={payload.get('exported_at')})...")

    complex_ids = await fetch("SELECT id FROM complexes")
    known_ids = {r["id"] for r in complex_ids}

    table_exists = await fetch(
        "SELECT 1 FROM information_schema.tables WHERE table_name = 'complex_relations'"
    )
    existing_pairs: set[tuple[int, int]] = set()
    if table_exists:
        existing = await fetch("SELECT complex_id_a, complex_id_b FROM complex_relations")
        existing_pairs = {(r["complex_id_a"], r["complex_id_b"]) for r in existing}
    else:
        print("NOTE: complex_relations table does not exist in this DB yet — "
              "migrations/095 not applied here. Existing-pair check skipped.")

    seen_in_file: set[tuple[int, int]] = set()
    results = []
    ok_rows = []

    for i, row in enumerate(rows):
        errors = []
        warnings = []
        a, b = row.get("complex_id_a"), row.get("complex_id_b")

        if a is None or b is None:
            errors.append("missing complex_id_a/b")
        elif not (a < b):
            errors.append(f"canonical order violated: complex_id_a={a} must be < complex_id_b={b}")
        if a is not None and a not in known_ids:
            errors.append(f"complex_id_a={a} not found in complexes")
        if b is not None and b not in known_ids:
            errors.append(f"complex_id_b={b} not found in complexes")

        rt = row.get("relation_type")
        if rt not in _ALLOWED_RELATION_TYPES:
            errors.append(f"relation_type={rt!r} not in allowed set {sorted(_ALLOWED_RELATION_TYPES)} "
                           "('ambiguous' is a review status, not a storable relation — see migrations/095)")

        conf = row.get("confidence")
        if not isinstance(conf, (int, float)) or not (0 <= conf <= 1):
            errors.append(f"confidence={conf!r} must be a number in [0, 1]")

        if not row.get("reviewed_by"):
            errors.append("reviewed_by is empty")
        try:
            datetime.fromisoformat(str(row.get("reviewed_at")).replace("Z", "+00:00"))
        except Exception:
            errors.append(f"reviewed_at={row.get('reviewed_at')!r} is not a valid ISO timestamp")

        if not isinstance(row.get("evidence"), dict) or not row.get("evidence"):
            errors.append("evidence must be a non-empty object")

        key = (a, b)
        if a is not None and b is not None:
            if key in seen_in_file:
                errors.append(f"duplicate pair ({a}, {b}) appears more than once in this import file")
            seen_in_file.add(key)
            if key in existing_pairs:
                warnings.append(f"pair ({a}, {b}) already has a row in complex_relations — "
                                 "this would be an UPDATE, not an INSERT (see migrations/095 docstring)")

        status = "ERROR" if errors else ("WARNING" if warnings else "OK")
        results.append({"index": i, "complex_id_a": a, "complex_id_b": b, "status": status,
                         "errors": errors, "warnings": warnings})
        if status in ("OK", "WARNING"):
            ok_rows.append(row)

        marker = "✓" if status == "OK" else ("⚠" if status == "WARNING" else "✗")
        print(f"  [{marker}] #{i} ({a} ↔ {b}) {rt} — {status}"
              + (f" — {'; '.join(errors + warnings)}" if errors or warnings else ""))

    n_ok = sum(1 for r in results if r["status"] == "OK")
    n_warn = sum(1 for r in results if r["status"] == "WARNING")
    n_err = sum(1 for r in results if r["status"] == "ERROR")
    print(f"\nsummary: {n_ok} OK, {n_warn} WARNING (pair already exists — would UPDATE), {n_err} ERROR")
    print(f"ambiguous_reviewed (never imported into complex_relations, by design): "
          f"{len(payload.get('ambiguous_reviewed', []))}")
    print(f"unreviewed_pairs remaining: {len(payload.get('unreviewed_pairs', []))}")

    sql_lines = [
        "-- complex_relations_import_preview.sql — NOT EXECUTED. Generated by",
        "-- scripts/import_reviewed_complex_relations.py for human review before",
        "-- manual apply. Rows with an existing pair use UPDATE (see migrations/095",
        "-- docstring: a pair has exactly one current relation, correction is an",
        "-- UPDATE of the existing row, not a second INSERT), all others use INSERT.",
        f"-- generated_at: {datetime.now(timezone.utc).isoformat()}",
        "",
    ]
    for row in ok_rows:
        a, b = row["complex_id_a"], row["complex_id_b"]
        evidence_json = json.dumps(row["evidence"], ensure_ascii=False)
        if (a, b) in existing_pairs:
            sql_lines.append(
                f"UPDATE complex_relations SET relation_type = {_sql_str(row['relation_type'])}, "
                f"confidence = {row['confidence']}, evidence = {_sql_str(evidence_json)}::jsonb, "
                f"reviewed_by = {_sql_str(row['reviewed_by'])}, reviewed_at = {_sql_str(row['reviewed_at'])}, "
                f"methodology_version = {_sql_str(row['methodology_version'])} "
                f"WHERE complex_id_a = {a} AND complex_id_b = {b};"
            )
        else:
            sql_lines.append(
                "INSERT INTO complex_relations (complex_id_a, complex_id_b, relation_type, confidence, "
                "evidence, reviewed_by, reviewed_at, methodology_version) VALUES "
                f"({a}, {b}, {_sql_str(row['relation_type'])}, {row['confidence']}, "
                f"{_sql_str(evidence_json)}::jsonb, {_sql_str(row['reviewed_by'])}, "
                f"{_sql_str(row['reviewed_at'])}, {_sql_str(row['methodology_version'])});"
            )

    sql_path = os.path.join(os.path.dirname(__file__), "..", "complex_relations_import_preview.sql")
    with open(sql_path, "w", encoding="utf-8") as f:
        f.write("\n".join(sql_lines) + "\n")
    print(f"\nSQL preview (NOT executed) written to {os.path.abspath(sql_path)}")

    report_path = os.path.join(os.path.dirname(__file__), "..", "complex_relations_import_validation_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump({
            "validated_at": datetime.now(timezone.utc).isoformat(),
            "n_ok": n_ok, "n_warning": n_warn, "n_error": n_err,
            "results": results,
        }, f, ensure_ascii=False, indent=2)
    print(f"validation report written to {os.path.abspath(report_path)}")
    print("\nНИЧЕГО не записано в БД — этот скрипт read-only от начала до конца.")


if __name__ == "__main__":
    asyncio.run(main())
