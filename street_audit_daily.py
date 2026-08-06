#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
street_audit_daily.py — автоматический аудит псевдо-ЖК-улиц (раз в сутки).

Вызывает bot.core.complex_audit.purge_street_complexes():
- находит «ЖК», у которых ≥60% объявлений имеют название в адресе (= улица);
- помечает их is_street=TRUE, обнуляет координаты;
- отвязывает их объявления (complex_name/complex_url → NULL);
- пересчитывает координаты остальных ЖК.

Лог: /tmp/street_audit.log. В crontab: 30 4 * * * (раз в день, ночь).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, "/home/nik/krisha_bot")
os.chdir("/home/nik/krisha_bot")

BASE = Path("/home/nik/krisha_bot")


def load_database_url() -> str:
    for line in (BASE / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip()
    return "postgresql://krisha@localhost/krisha_bot"


async def main() -> None:
    from bot.db.pg import init_pool, close_pool
    from bot.core.complex_audit import purge_street_complexes

    await init_pool(load_database_url())
    try:
        res = await purge_street_complexes()
        print(json.dumps(res, ensure_ascii=False, default=str), flush=True)
        flagged = res.get("flagged", 0)
        unbound = res.get("unbound", 0)
        if flagged:
            print(f"Помечено улиц: {flagged}, отвязано объявлений: {unbound}", flush=True)
        else:
            print("Улиц не найдено — всё чисто.", flush=True)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
