#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Реестр КЖК (задача 2026-08-15, блок 7 docs/liquidity_model_design.md,
§3.3 docs/strategic_independence.md) — сбор с developers.kz/market/
proverit-zastroyshika, ЕДИНСТВЕННАЯ нужная страница (см. migrations/
074_kzk_registry.sql докстринг за разведку): весь реестр (313 записей
на дату разведки) встроен в HTML как JSON (`<script id="regBase">`),
обычный GET, без пагинации/Playwright/повторных запросов на
застройщика.

**Схема НА УРОВНЕ ЗАСТРОЙЩИКА, не проекта** (см. migrations/074) —
project_address/apartments_total/warranty_number и т.п. на источнике
физически нет, не пишем несуществующие поля.

Обновляется на источнике редко (на разведке "Данные обновлены" было на
~2.5 недели раньше даты разведки) — таймер еженедельно, не чаще (то же
соображение, что уже применено к Overpass в других парсерах: не
дёргаем чаще, чем реально меняется источник).

Upsert по `bin` (UNIQUE, migrations/074) — обновляет все поля +
fetched_at при повторном прогоне, не плодит дубли. "Удалённые" (bin
был у нас, пропал из свежего снапшота) — только логируются, НЕ
удаляются из БД: это текущий срез с внешнего сайта, не наш append-only
журнал, но исчезновение записи может быть временным сбоем вёрстки
источника, а не реальным удалением застройщика — решение об удалении
оставлено человеку, не автоматике.

Расписание: krisha-kzk-registry.timer (еженедельно).
Разовая проверка: venv/bin/python kzk_registry_collect.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import date

import httpx
from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.FileHandler("kzk_registry_collect.log", encoding="utf-8", errors="replace")],
)
log = logging.getLogger("kzk_registry_collect")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://krisha:123@localhost/krisha_bot")

SOURCE_URL = "https://developers.kz/market/proverit-zastroyshika"
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

_REGBASE_RE = re.compile(r'<script type="application/json" id="regBase">(.*?)</script>', re.S)
# "Данные обновлены <b>29.07.2026</b>" — дата снапшота НА САЙТЕ, не когда
# мы его забрали (см. migrations/074 про source_snapshot_date/fetched_at).
_SNAPSHOT_DATE_RE = re.compile(r"Данные обновлены\s*<b>(\d{2})\.(\d{2})\.(\d{4})</b>")


def parse_registry_html(html: str) -> tuple[list[dict], date | None]:
    """Возвращает (записи, дата снапшота с сайта) — чистая функция, без
    сети/БД, тестируется на сохранённом HTML напрямую."""
    m = _REGBASE_RE.search(html)
    if not m:
        raise ValueError("regBase JSON не найден в HTML — вёрстка developers.kz могла измениться")
    entries = json.loads(m.group(1))

    snapshot_date = None
    dm = _SNAPSHOT_DATE_RE.search(html)
    if dm:
        d, mo, y = dm.groups()
        snapshot_date = date(int(y), int(mo), int(d))
    return entries, snapshot_date


async def fetch_registry_html() -> str:
    async with httpx.AsyncClient(timeout=30.0, headers=_HEADERS, follow_redirects=True) as client:
        resp = await client.get(SOURCE_URL)
        resp.raise_for_status()
        return resp.text


_COMPARE_COLS = ("developer_legal", "developer_brand", "cities", "objects_count", "zhk_count",
                  "by_city", "warranty_scheme", "is_blacklisted", "in_registry", "zhk_names", "phone")


def _new_values(e: dict) -> dict:
    """Значения записи в ТОМ ЖЕ виде, что уйдёт в UPSERT — используется и
    для самой записи, и для сравнения new vs existing (created/updated/
    unchanged, задача 2026-08-17 п.3: "Покажи counts created/updated/
    unchanged/skipped/errors")."""
    return {
        "developer_legal": e.get("dev") or "", "developer_brand": e.get("brand"),
        "cities": e.get("cities") or [], "objects_count": e.get("objects"),
        "zhk_count": e.get("zhk_n"), "by_city": e.get("by_city") or [],
        "warranty_scheme": e.get("scheme") or None,
        # is_blacklisted = сырое "flagged" КАК ЕСТЬ, не AND с in_reg (см.
        # migrations/074 докстринг про пограничный случай "flagged=true
        # И in_reg=true" разом).
        "is_blacklisted": bool(e.get("flagged")), "in_registry": bool(e.get("in_reg")),
        "zhk_names": e.get("zhk") or [], "phone": e.get("phone"),
    }


def _parse_existing_row(row: dict) -> dict:
    """asyncpg без кастомного codec отдаёт jsonb как raw text — парсим в
    Python-объекты, чтобы сравнение new vs existing было СЕМАНТИЧЕСКИМ
    (списки/словари), а не строковым (сериализация Postgres JSONB и
    python json.dumps форматируют по-разному даже для одинаковых
    данных — строковое сравнение давало бы ложные "updated")."""
    out = dict(row)
    for col in ("cities", "by_city", "zhk_names"):
        v = out.get(col)
        if isinstance(v, str):
            out[col] = json.loads(v)
    return out


async def run_collect(html: str | None = None, dry_run: bool = False) -> dict:
    """html — опциональный override ТОЛЬКО для тестов (тот же паттерн
    скоупинга/подмены, что у остальных collect/snapshot-скриптов этой
    сессии) — прод-путь (None) всегда идёт живым GET-запросом.

    dry_run — задача 2026-08-17 ("KZK registry нужен... сначала dry-run/
    canary"): живой GET + парсинг ВСЕГДА выполняются (это и есть
    "проверить endpoint, формат ответа" на канарейке), но UPSERT в
    kzk_registry пропускается — created/updated/unchanged считаются
    АНАЛИТИЧЕСКИ (сравнение с уже сохранённой строкой), не по факту
    записи.

    Каждая запись обрабатывается в своём try/except — одна "кривая"
    запись (errors_count) не должна прерывать весь прогон (задача,
    неявно — "покажи errors" подразумевает, что они возможны и не
    фатальны для остальных 300+ записей)."""
    from bot.db.pg import fetch, execute

    if html is None:
        html = await fetch_registry_html()
    entries, snapshot_date = parse_registry_html(html)

    existing_rows = {
        r["bin"]: _parse_existing_row(dict(r))
        for r in await fetch(f"SELECT bin, {', '.join(_COMPARE_COLS)} FROM kzk_registry")
    }
    new_bins = {e["bin"] for e in entries if e.get("bin")}
    removed_bins = set(existing_rows) - new_bins

    created_count = updated_count = unchanged_count = skipped_count = errors_count = 0
    error_samples: list[str] = []
    for e in entries:
        try:
            bin_ = e.get("bin")
            if not bin_:
                skipped_count += 1
                continue

            new_vals = _new_values(e)
            existing = existing_rows.get(bin_)
            if existing is None:
                classification = "created"
            elif all(new_vals[c] == existing[c] for c in _COMPARE_COLS):
                classification = "unchanged"
            else:
                classification = "updated"

            if dry_run:
                # Классификация — валидный "что было бы" результат уже
                # здесь (никакого запроса, который мог бы упасть, дальше
                # не будет для ЭТОЙ записи).
                if classification == "created":
                    created_count += 1
                elif classification == "unchanged":
                    unchanged_count += 1
                else:
                    updated_count += 1
                continue

            await execute("""
                INSERT INTO kzk_registry
                    (bin, developer_legal, developer_brand, cities, objects_count, zhk_count,
                     by_city, warranty_scheme, is_blacklisted, in_registry, zhk_names, phone,
                     source_snapshot_date, fetched_at)
                VALUES ($1,$2,$3,$4::jsonb,$5,$6,$7::jsonb,$8,$9,$10,$11::jsonb,$12,$13,now())
                ON CONFLICT (bin) DO UPDATE SET
                    developer_legal=EXCLUDED.developer_legal, developer_brand=EXCLUDED.developer_brand,
                    cities=EXCLUDED.cities, objects_count=EXCLUDED.objects_count, zhk_count=EXCLUDED.zhk_count,
                    by_city=EXCLUDED.by_city, warranty_scheme=EXCLUDED.warranty_scheme,
                    is_blacklisted=EXCLUDED.is_blacklisted, in_registry=EXCLUDED.in_registry,
                    zhk_names=EXCLUDED.zhk_names, phone=EXCLUDED.phone,
                    source_snapshot_date=EXCLUDED.source_snapshot_date, fetched_at=now()
            """,
                bin_, new_vals["developer_legal"], new_vals["developer_brand"],
                json.dumps(new_vals["cities"]), new_vals["objects_count"], new_vals["zhk_count"],
                json.dumps(new_vals["by_city"]), new_vals["warranty_scheme"],
                new_vals["is_blacklisted"], new_vals["in_registry"],
                json.dumps(new_vals["zhk_names"]), new_vals["phone"], snapshot_date,
            )
            # Счётчик — ПОСЛЕ успешного execute(): если INSERT упал (see
            # except ниже), классификация этой записи не должна попасть
            # в created/updated/unchanged — только в errors (иначе
            # "created" завышался бы записями, которые на самом деле не
            # записались).
            if classification == "created":
                created_count += 1
            elif classification == "unchanged":
                unchanged_count += 1
            else:
                updated_count += 1
        except Exception as exc:
            # Одна кривая запись НЕ должна стереть/остановить сбор
            # остальных ~300 (задача: "ошибка внешнего API не должна
            # стирать или обнулять ранее собранные данные" — тот же
            # принцип применяем и к ошибке ОДНОЙ записи внутри иначе
            # успешного снапшота).
            errors_count += 1
            if len(error_samples) < 10:
                error_samples.append(f"bin={e.get('bin')}: {exc}")
            log.warning("kzk_registry_collect: запись bin=%s упала: %s", e.get("bin"), exc, exc_info=True)

    log.info(
        "kzk_registry_collect: %d записей в снапшоте (snapshot_date=%s, dry_run=%s), "
        "created=%d updated=%d unchanged=%d skipped=%d errors=%d, "
        "%d пропали из свежего снапшота (НЕ удалены из БД — только залогировано): %s",
        len(entries), snapshot_date, dry_run, created_count, updated_count, unchanged_count,
        skipped_count, errors_count, len(removed_bins), sorted(removed_bins)[:20],
    )
    return {
        "total": len(entries), "dry_run": dry_run,
        "created": created_count, "updated": updated_count, "unchanged": unchanged_count,
        "skipped": skipped_count, "errors": errors_count, "error_samples": error_samples,
        "removed": len(removed_bins), "removed_bins": sorted(removed_bins),
        "snapshot_date": str(snapshot_date) if snapshot_date else None,
    }


async def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                     help="живой GET + парсинг источника выполняются, UPSERT в БД — нет, "
                          "counts created/updated/unchanged — аналитические")
    args = ap.parse_args()

    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    try:
        result = await run_collect(dry_run=args.dry_run)
    finally:
        await close_pool()
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
