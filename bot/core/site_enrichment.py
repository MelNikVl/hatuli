"""
Общая инфраструктура для обогащения complexes с внешних агрегаторов
(Korter, Homsters, ...). Каждый источник пишет в свой раздел source_info
(JSONB, не перезатирая другие источники) + при желании в общие колонки
(housing_class и т.п.) через COALESCE — первый источник, который узнал
факт, не перезаписывается менее уверенным.
"""
from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)


def norm_name(name: str) -> str:
    n = name.lower()
    n = re.sub(r"^(жк|кг|жилой комплекс|жилой массив|коттеджный городок|мкр)\.?\s+", "", n)
    n = re.sub(r"[«»\"'()]", "", n)
    return re.sub(r"\s+", " ", n).strip()


async def save_enrichment(found: dict[str, dict], source_key: str,
                          set_housing_class: bool = False) -> int:
    """
    found: {norm_name: {..поля..}}
    source_key: 'korter' | 'homsters' | ...
    Пишет found[key] в complexes.source_info->{source_key}, мержа с уже
    существующими данными от других источников. housing_class/korter_url
    обновляются через COALESCE только если set_housing_class=True (чтобы
    не путать источники разного качества).
    """
    from bot.db.pg import fetch, execute, fetchrow

    ours = await fetch("SELECT id, name FROM complexes")
    by_norm = {norm_name(r["name"]): r["id"] for r in ours if r["name"]}

    matched = 0
    for key, data in found.items():
        cid = by_norm.get(key)
        if not cid:
            continue
        matched += 1
        row = await fetchrow("SELECT source_info FROM complexes WHERE id=$1", cid)
        existing = {}
        if row and row["source_info"]:
            existing = row["source_info"] if isinstance(row["source_info"], dict) else json.loads(row["source_info"])
        existing[source_key] = data

        if set_housing_class and data.get("housing_class"):
            await execute(
                """UPDATE complexes SET
                     housing_class = COALESCE(housing_class, $2),
                     korter_url    = COALESCE(korter_url, $3),
                     source_info   = $4::jsonb,
                     updated_at    = now()
                   WHERE id = $1""",
                cid, data.get("housing_class"), data.get("url"),
                json.dumps(existing, ensure_ascii=False, default=str),
            )
        else:
            await execute(
                """UPDATE complexes SET source_info = $2::jsonb, updated_at = now()
                   WHERE id = $1""",
                cid, json.dumps(existing, ensure_ascii=False, default=str),
            )
    logger.info("%s: matched %d/%d ЖК", source_key, matched, len(found))
    return matched
