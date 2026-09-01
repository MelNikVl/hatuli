#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/build_complex_relation_review_examples.py — задача 2026-08-31,
"Complex Identity: human labeling + impact assessment" (следующий шаг
после feat/complex-identity-review-layer, PR #48). Read-only enrichment
поверх scripts/build_complex_relation_review_dataset.py — НЕ новая
эвристика классификации (candidate_relation не трогается, не
пересчитывается), только добавляет "examples" — по 2 примера listing'ов
на каждую сторону пары (id/url/title/price/is_active), которых не было в
top_100 датасете, но которые явно нужны human review — reviewer должен
иметь возможность открыть реальное объявление и посмотреть глазами, а
не верить агрегированным полям.

Вход: complex_relation_review_top100.json (из build_complex_relation_
review_dataset.py — должен быть сгенерирован первым).
Выход: complex_relation_review_top100_enriched.json — те же 100 записей
+ "examples_a"/"examples_b". НЕ коммитится в git (тот же локальный
review-артефакт паттерн, что и родительский датасет).

Приоритет примеров: активные (is_active=TRUE) сначала, затем по
last_seen desc — чтобы reviewer видел то, что реально на рынке сейчас,
не архивный шум трёхлетней давности.

    venv/bin/python scripts/build_complex_relation_review_examples.py
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

_IN_PATH = os.path.join(os.path.dirname(__file__), "..", "complex_relation_review_top100.json")
_OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "complex_relation_review_top100_enriched.json")
_EXAMPLES_PER_SIDE = 3


async def main() -> None:
    from bot.db.pg import close_pool, fetch, init_pool
    await init_pool(DATABASE_URL)
    try:
        await run(fetch)
    finally:
        await close_pool()


async def run(fetch) -> None:
    with open(_IN_PATH, encoding="utf-8") as f:
        data = json.load(f)

    names = set()
    for r in data["top_100"]:
        names.add(r["name_a"])
        names.add(r["name_b"])

    rows = await fetch(
        "SELECT id, complex_name, url, title, price, area, rooms, floor, is_active, last_seen "
        "FROM apartment_listings WHERE complex_name = ANY($1::text[])",
        list(names),
    )
    by_name: dict[str, list[dict]] = {}
    for r in rows:
        by_name.setdefault(r["complex_name"], []).append(dict(r))
    for name, lst in by_name.items():
        lst.sort(key=lambda r: (not r["is_active"], r["last_seen"] or ""), reverse=False)

    def _examples(name: str) -> list[dict]:
        out = []
        for r in by_name.get(name, [])[:_EXAMPLES_PER_SIDE]:
            out.append({
                "listing_id": r["id"], "url": r["url"], "title": r["title"],
                "price": r["price"], "area": r["area"], "rooms": r["rooms"], "floor": r["floor"],
                "is_active": r["is_active"],
            })
        return out

    n_with_examples_both = 0
    for r in data["top_100"]:
        r["examples_a"] = _examples(r["name_a"])
        r["examples_b"] = _examples(r["name_b"])
        if r["examples_a"] and r["examples_b"]:
            n_with_examples_both += 1

    with open(_OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)

    print(f"enriched {len(data['top_100'])} pairs with up to {_EXAMPLES_PER_SIDE} example listings per side")
    print(f"pairs with at least one example on BOTH sides: {n_with_examples_both}")
    print(f"written to {os.path.abspath(_OUT_PATH)} (NOT committed to git — local review artifact)")
    print("НИЧЕГО не записано в БД.")


if __name__ == "__main__":
    asyncio.run(main())
