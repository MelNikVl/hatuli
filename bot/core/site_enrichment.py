"""
Общая инфраструктура для обогащения complexes с внешних агрегаторов
(Korter, Homsters, ...). Каждый источник пишет в свой раздел source_info
(JSONB, не перетирая другие источники) + при желании в общие колонки
(housing_class и т.п.) через COALESCE — первый источник, который узнал
факт, не перезаписывается менее уверенным.

Изменения относительно предыдущего прогона логируются в source_changes
(новые ЖК, изменённые поля), каждый прогон — в source_runs (длительность
полного обхода). Оба читаются вкладками Korter/Homsters страницы
/admin/parsers.
"""
from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

# Поля, которые не считаем «изменением параметров ЖК» (технические/ключ).
_SKIP_DIFF_FIELDS = {"url", "name"}


def norm_name(name: str) -> str:
    n = name.lower()
    n = re.sub(r"^(жк|кг|жилой комплекс|жилой массив|коттеджный городок|мкр)\.?\s+", "", n)
    n = re.sub(r"[«»\"'()]", "", n)
    return re.sub(r"\s+", " ", n).strip()


def _fmt(v) -> str | None:
    """Человекочитаемое строковое представление значения для diff."""
    if v is None:
        return None
    if isinstance(v, float):
        return str(int(v)) if v == int(v) else str(v)
    return str(v)


async def save_enrichment(found: dict[str, dict], source_key: str,
                          set_housing_class: bool = False) -> dict:
    """
    found: {norm_name: {..поля..}}
    source_key: 'korter' | 'homsters' | ...
    Пишет found[key] в complexes.source_info->{source_key}, мержа с уже
    существующими данными от других источников. housing_class/korter_url
    обновляются через COALESCE только если set_housing_class=True (чтобы
    не путать источники разного качества).

    Возвращает {"matched":.., "created":.., "changed":..}: сопоставлено ЖК,
    создано заново (из каталога источника), записей изменений. Новые ЖК и
    изменённые поля пишутся в source_changes (первичное наполнение ЖК
    данными источника — НЕ считается изменением: diff идёт только когда у
    ЖК уже были данные этого источника).
    """
    from bot.db.pg import fetchval, fetch, execute, fetchrow

    ours = await fetch("SELECT id, name FROM complexes")
    by_norm = {norm_name(r["name"]): r["id"] for r in ours if r["name"]}

    matched = created = 0
    change_rows: list[tuple] = []  # (complex_id, complex_name, change_type, field, old, new)

    for key, data in found.items():
        cid = by_norm.get(key)
        is_new = False
        if not cid:
            # ЖК из каталога источника, которого у нас ещё нет — создаём:
            # иначе весь каталог korter/homsters по ЖК без наших объявлений
            # (новостройки без вторички, дорогие ЖК) просто отбрасывался.
            try:
                cid = await fetchval(
                    "INSERT INTO complexes (name, district) VALUES ($1, $2) RETURNING id",
                    data.get("name") or key, data.get("district"))
                by_norm[key] = cid
                created += 1
                is_new = True
            except Exception as e:
                logger.warning("create complex %s failed: %s", key, e)
                continue
        matched += 1

        row = await fetchrow("SELECT source_info FROM complexes WHERE id=$1", cid)
        existing = {}
        if row and row["source_info"]:
            existing = row["source_info"] if isinstance(row["source_info"], dict) else json.loads(row["source_info"])
        old_data = existing.get(source_key) or {}
        had_old = source_key in existing
        existing[source_key] = data

        if is_new:
            change_rows.append((cid, data.get("name") or key, "new", None, None, None))
        elif had_old:
            for f in sorted(set(old_data) | set(data)):
                if f in _SKIP_DIFF_FIELDS:
                    continue
                if _fmt(old_data.get(f)) != _fmt(data.get(f)):
                    change_rows.append((cid, data.get("name") or key, "updated",
                                        f, _fmt(old_data.get(f)), _fmt(data.get(f))))

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

    if change_rows:
        for cid, cname, ctype, field, old, new in change_rows:
            try:
                await execute(
                    """INSERT INTO source_changes
                       (source, complex_id, complex_name, change_type, field, old_value, new_value)
                       VALUES ($1,$2,$3,$4,$5,$6,$7)""",
                    source_key, cid, cname, ctype, field, old, new)
            except Exception as e:
                logger.warning("source_changes insert failed: %s", e)

    logger.info("%s: matched %d/%d ЖК, создано %d, изменений %d",
                source_key, matched, len(found), created, len(change_rows))
    return {"matched": matched, "created": created, "changed": len(change_rows)}


async def record_run(source: str, started_at, duration_s,
                     matched: int, created: int, changed: int) -> None:
    """Фиксирует прогон источника: длительность полного обхода + счётчики."""
    from bot.db.pg import execute
    try:
        await execute(
            """INSERT INTO source_runs (source, started_at, duration_s, matched, created, changed)
               VALUES ($1,$2,$3,$4,$5,$6)""",
            source, started_at, duration_s, matched, created, changed)
    except Exception as e:
        logger.warning("source_runs insert failed: %s", e)
