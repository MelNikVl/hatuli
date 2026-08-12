#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Разовая чистка очереди (задача 2026-08-12, см. docs/entity_resolution_plan.md):
21 review-кандидат от bazis/orda_invest, обнаруженный живой калибровкой
после фикса токена фазы — не настоящая неопределённость, а повторное
предложение УЖЕ подтверждённой в spine пары (тот же complex_id, там же
на confidence 1.0/legacy_import; у bazis/orda_invest confidence
пересчёта ниже auto только потому, что эти два импортёра не отдают
гео/адрес вообще — см. entity_resolution_plan.md).

approve_candidate() тут не годится — переписал бы spine на более низкую
confidence (0.75 вместо 1.0), регресс. reject_candidate() тоже не
годится — запомнил бы пару как ОТКЛОНЁННУЮ навсегда, хотя она верна,
просто избыточна; заблокировал бы будущую переоценку, если bazis/
orda_invest когда-нибудь начнут отдавать гео.

Правильное действие — прямое удаление строки кандидата, ничего не
трогая в spine. record_source_link() с этого коммита сам не создаёт
такие кандидаты (предохранитель 'already_linked'), так что это
разовая чистка уже существующего мусора, не постоянный скрипт.

Идемпотентен (WHERE подразумевает "то же условие", что породило
проблему) — безопасно перезапускать, вторая попытка найдёт 0 строк.
"""
import subprocess


def psql(sql: str) -> str:
    r = subprocess.run(
        ["sudo", "-u", "postgres", "psql", "-d", "krisha_bot", "-t", "-A", "-F", "\t", "-c", sql],
        capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:800])
    return r.stdout


SELECT_SQL = """
    SELECT c.id, cx.name, c.source, c.source_id, c.confidence, c.match_method
    FROM complex_source_link_candidates c
    JOIN complexes cx ON cx.id = c.complex_id
    JOIN complex_source_links l ON l.source = c.source AND l.source_id = c.source_id
    WHERE c.kind = 'review'
      AND l.complex_id = c.complex_id   -- тот же ЖК уже подтверждён в spine
      AND l.confidence >= c.confidence  -- и с не меньшей уверенностью — не теряем сигнал
    ORDER BY c.source, cx.name
"""

DELETE_SQL = """
    DELETE FROM complex_source_link_candidates c
    USING complex_source_links l
    WHERE c.kind = 'review'
      AND l.source = c.source AND l.source_id = c.source_id
      AND l.complex_id = c.complex_id
      AND l.confidence >= c.confidence
"""

print("К удалению (review-кандидаты, дублирующие уже подтверждённую в spine связь):")
rows = [r for r in psql(SELECT_SQL).splitlines() if r.strip()]
for r in rows:
    cid, name, source, source_id, confidence, method = r.split("\t")
    print(f"  #{cid:>4}  {source:12} {source_id:25} -> {name:30} conf={confidence} ({method})")
print(f"итого: {len(rows)}")

if rows:
    psql(DELETE_SQL)
    print(f"удалено: {len(rows)}")
else:
    print("удалять нечего (повторный запуск — уже почищено)")
