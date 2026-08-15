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


async def run_collect(html: str | None = None) -> dict:
    """html — опциональный override ТОЛЬКО для тестов (тот же паттерн
    скоупинга/подмены, что у остальных collect/snapshot-скриптов этой
    сессии) — прод-путь (None) всегда идёт живым GET-запросом."""
    from bot.db.pg import fetch, execute

    if html is None:
        html = await fetch_registry_html()
    entries, snapshot_date = parse_registry_html(html)

    existing_bins = {r["bin"] for r in await fetch("SELECT bin FROM kzk_registry")}
    new_bins = {e["bin"] for e in entries if e.get("bin")}
    removed_bins = existing_bins - new_bins

    new_count = updated_count = 0
    for e in entries:
        bin_ = e.get("bin")
        if not bin_:
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
            bin_, e.get("dev") or "", e.get("brand"),
            json.dumps(e.get("cities") or []), e.get("objects"), e.get("zhk_n"),
            json.dumps(e.get("by_city") or []), e.get("scheme") or None,
            # is_blacklisted = сырое "flagged" КАК ЕСТЬ, не AND с in_reg
            # (см. migrations/074 докстринг про пограничный случай
            # "flagged=true И in_reg=true" разом).
            bool(e.get("flagged")), bool(e.get("in_reg")),
            json.dumps(e.get("zhk") or []), e.get("phone"), snapshot_date,
        )
        if bin_ in existing_bins:
            updated_count += 1
        else:
            new_count += 1

    log.info(
        "kzk_registry_collect: %d записей в снапшоте (snapshot_date=%s), %d новых, %d обновлено, "
        "%d пропали из свежего снапшота (НЕ удалены из БД — только залогировано): %s",
        len(entries), snapshot_date, new_count, updated_count, len(removed_bins),
        sorted(removed_bins)[:20],
    )
    return {
        "total": len(entries), "new": new_count, "updated": updated_count,
        "removed": len(removed_bins), "removed_bins": sorted(removed_bins),
        "snapshot_date": str(snapshot_date) if snapshot_date else None,
    }


async def main() -> None:
    from bot.db.pg import init_pool, close_pool
    await init_pool(DATABASE_URL)
    try:
        await run_collect()
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
