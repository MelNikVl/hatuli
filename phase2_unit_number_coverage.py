#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Покрытие номера квартиры на обеих сторонах (задача фазы 2, п.Б —
"сначала парсинг номера квартиры", 2026-08-13). Извлечение — разное на
каждый источник (нет общего поля), см. _unit_number_newbuild()/
_unit_number_listing(). Отчёт по факту на реальных 28 ЖК пересечения
(см. docs/entity_resolution_plan.md, numbers-first проход), не
абстрактно.

Запуск: venv/bin/python phase2_unit_number_coverage.py
"""
import asyncio
import json
import re
import sys

sys.path.insert(0, ".")
from dotenv import load_dotenv
load_dotenv()
import os

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

_ORDA_TITLE_RE = re.compile(r"№\s*(?:кв\s*)?(\d+)", re.I)
_SVOYDOM_ID_RE = re.compile(r"-(\d+)-[\d.]+$")
_LISTING_DESC_RE = re.compile(r"№\s*(\d{1,4})\b")


def unit_number_newbuild(source: str, raw_json_text: str | None, source_unit_id: str | None) -> str | None:
    """Номер квартиры со стороны застройщика — поле разное у каждого
    источника (не общий формат): sensata 'number', bazis 'kv'->'Кв. №',
    nak 'nomer', bi_group 'name' (у BI Group это и есть номер, не uuid),
    orda_invest — из title-текста ('№83'/'№кв 83'), svoydom — из
    source_unit_id (сам номер не в raw_json — там вообще пусто у этого
    источника, см. живую находку 2026-08-13), формат
    'sv-{house}-{секция}-{номер}-{площадь}' — номер предпоследний
    сегмент перед площадью."""
    if source == "svoydom":
        if not source_unit_id:
            return None
        m = _SVOYDOM_ID_RE.search(source_unit_id)
        return m.group(1) if m else None

    if not raw_json_text:
        return None
    try:
        data = json.loads(raw_json_text) if isinstance(raw_json_text, str) else raw_json_text
    except (json.JSONDecodeError, TypeError):
        return None

    if source == "sensata":
        v = data.get("number")
        return str(v).strip() if v not in (None, "") else None
    if source == "bazis":
        v = (data.get("kv") or {}).get("Кв. №")
        return str(v).strip() if v not in (None, "") else None
    if source == "nak":
        v = data.get("nomer")
        return str(v).strip() if v not in (None, "") else None
    if source == "bi_group":
        v = data.get("name")
        return str(v).strip() if v not in (None, "") else None
    if source == "orda_invest":
        title = data.get("title") or ""
        m = _ORDA_TITLE_RE.search(title)
        return m.group(1) if m else None
    return None


def unit_number_listing(description: str | None, title: str | None) -> str | None:
    """Номер квартиры со стороны Крыши — не структурное поле, только
    из свободного текста объявления ("№106"). Более узкий паттерн, чем
    'кв\\.?\\s*№?\\s*\\d+' (тот ловил шум — "кв 2026" из фраз про год
    сдачи ключей) — только '№N', самый надёжный маркер на практике."""
    for text in (description, title):
        if not text:
            continue
        m = _LISTING_DESC_RE.search(text)
        if m:
            return m.group(1)
    return None


async def main():
    from bot.db.pg import init_pool, close_pool, fetch

    await init_pool(DATABASE_URL)

    nb_rows = await fetch("""
        SELECT nu.id, nu.source, nu.raw_json::text AS raw_json, nu.source_unit_id
        FROM newbuild_units nu
        JOIN complexes c ON c.id = nu.complex_id
        JOIN apartment_listings al ON lower(trim(al.complex_name)) = lower(trim(c.name)) AND al.is_new_build = TRUE
    """)
    by_source: dict[str, list[int]] = {}
    covered_by_source: dict[str, int] = {}
    for r in nb_rows:
        by_source.setdefault(r["source"], []).append(r["id"])
        if unit_number_newbuild(r["source"], r["raw_json"], r["source_unit_id"]):
            covered_by_source[r["source"]] = covered_by_source.get(r["source"], 0) + 1

    print("=== newbuild_units (сторона застройщика) ===")
    for source, ids in sorted(by_source.items()):
        cov = covered_by_source.get(source, 0)
        print(f"  {source}: {cov}/{len(ids)} ({100*cov/len(ids):.0f}%)")

    al_rows = await fetch("""
        SELECT al.id, al.description, al.title
        FROM apartment_listings al
        JOIN complexes c ON lower(trim(al.complex_name)) = lower(trim(c.name))
        JOIN newbuild_units nu ON nu.complex_id = c.id
        WHERE al.is_new_build = TRUE
        GROUP BY al.id, al.description, al.title
    """)
    al_covered = sum(1 for r in al_rows if unit_number_listing(r["description"], r["title"]))
    print(f"\n=== apartment_listings (сторона Крыши, is_new_build) ===")
    print(f"  {al_covered}/{len(al_rows)} ({100*al_covered/len(al_rows):.0f}%)")

    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
