#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Entity resolution, фаза 1 (задача 2026-08-12, см. docs/entity_resolution_plan.md).

Разовый backfill:
  1) complexes.code для всех строк, где его ещё нет (JK-000123).
  2) complex_source_links из уже существующих однослотовых полей —
     krisha_url, korter_url, newbuild_source(+_id), homeportal_objects.
     Всё это уже руками/импортом подтверждённые связи -> match_method
     'manual', confidence 1.0.

source_info (korter/homsters JSONB-блоки без выделенного id) сознательно
НЕ разбираем здесь — см. план, "могут остаться на старой схеме, если у
блоков нет id". Дальше новые связи пишет
bot/core/entity_resolution.record_source_link() из ensure_complex()
при каждом прогоне парсеров новостроек.

Идемпотентен — ON CONFLICT (source, source_id) DO NOTHING, можно
перезапускать сколько угодно раз.
"""
import subprocess


def psql(sql: str) -> str:
    r = subprocess.run(
        ['sudo', '-u', 'postgres', 'psql', '-d', 'krisha_bot', '-t', '-A', '-F', '\t', '-c', sql],
        capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:800])
    return r.stdout


def esc(s: str | None) -> str:
    return (s or '').replace("'", "''")


def backfill_from(select_sql: str, cols: list[str], *, source_col: str | None = None,
                   source_literal: str | None = None, id_col: str, cid_col: str = "id",
                   url_col: str | None = None, matched_at_col: str | None = None,
                   label: str = "") -> int:
    """cols — имена колонок в том же порядке, что в select_sql (для
    сплита \\t-строк из psql -A -F). source_literal ИЛИ source_col — одно
    из двух: фиксированное имя источника или колонка с ним (newbuild_source)."""
    rows = [r for r in psql(select_sql).splitlines() if r.strip()]
    n = 0
    for line in rows:
        d = dict(zip(cols, line.split('\t')))
        cid = d[cid_col]
        src = source_literal if source_literal else d[source_col]
        sid = d[id_col]
        if not sid:
            continue
        url = f"'{esc(d[url_col])}'" if url_col and d.get(url_col) else "NULL"
        matched_at = f"'{esc(d[matched_at_col])}'" if matched_at_col and d.get(matched_at_col) else "now()"
        psql(f"""INSERT INTO complex_source_links
                    (complex_id, source, source_id, url, match_method, confidence, matched_by, matched_at)
                 VALUES ({cid}, '{esc(src)}', '{esc(sid)}', {url}, 'manual', 1.0, 'backfill_2026-08-12', {matched_at})
                 ON CONFLICT (source, source_id) DO NOTHING""")
        n += 1
    print(f"{label}: {n}")
    return n


print("1) code для ЖК без кода...")
psql("UPDATE complexes SET code = 'JK-' || lpad(id::text, 6, '0') WHERE code IS NULL")
print("   готово")

print("2) krisha_url...")
backfill_from(
    "SELECT id, krisha_url FROM complexes WHERE krisha_url IS NOT NULL AND krisha_url != ''",
    ["id", "krisha_url"], source_literal="krisha", id_col="krisha_url",
    url_col="krisha_url", label="krisha_url")

print("3) korter_url...")
backfill_from(
    "SELECT id, korter_url FROM complexes WHERE korter_url IS NOT NULL AND korter_url != ''",
    ["id", "korter_url"], source_literal="korter", id_col="korter_url",
    url_col="korter_url", label="korter_url")

print("4) newbuild_source + newbuild_source_id...")
backfill_from(
    """SELECT id, newbuild_source, newbuild_source_id FROM complexes
       WHERE newbuild_source IS NOT NULL AND newbuild_source_id IS NOT NULL""",
    ["id", "newbuild_source", "newbuild_source_id"],
    source_col="newbuild_source", id_col="newbuild_source_id", label="newbuild_source")

print("5) homeportal_objects (matched_complex_id -> object_id)...")
backfill_from(
    """SELECT matched_complex_id AS id, object_id, matched_at FROM homeportal_objects
       WHERE matched_complex_id IS NOT NULL""",
    ["id", "object_id", "matched_at"],
    source_literal="homeportal", id_col="object_id", matched_at_col="matched_at", label="homeportal")

print("done")
