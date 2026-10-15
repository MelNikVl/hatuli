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
    from bot.db.pg import get_pool, execute
    from bot.core.complex_ingest_identity import (
        AmbiguousComplexIdentity, InvalidComplexIdentity, canonical_complex_id,
        record_complex_observations, resolve_ingest_complex,
    )

    matched = created = 0
    change_rows: list[tuple] = []  # (complex_id, complex_name, change_type, field, old, new)

    for key, data in found.items():
        name = data.get("name") or key
        source_id = str(data.get("source_id") or data.get("id") or data.get("url") or key)
        is_new = False
        try:
            async with get_pool().acquire() as conn:
                async with conn.transaction():
                    name_key = await conn.fetchval("SELECT complex_name_key($1)", name)
                    for lock_key in sorted({f"complex_ingest:source:{source_key}:{source_id}",
                                            f"complex_ingest:name:{name_key}"}):
                        await conn.fetchval("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", lock_key)
                    cid = await resolve_ingest_complex(source_key, source_id, name, connection=conn)
                    if cid is None:
                        cid = await conn.fetchval("""
                            INSERT INTO complexes (name, district) VALUES ($1, $2)
                            ON CONFLICT (lower(name)) DO NOTHING RETURNING id
                        """, name, data.get("district"))
                        if cid is None:
                            cid = await resolve_ingest_complex(source_key, source_id, name, connection=conn)
                            if cid is None:
                                raise InvalidComplexIdentity(f"Existing excluded complex blocks {name!r}")
                        else:
                            is_new = True
                    evidence = {"url": data.get("url"), "name": name, "normalized_name": name_key,
                                "identity_method": "source_id_or_unique_exact_or_verified_alias"}
                    await conn.execute("""
                        INSERT INTO complex_source_links
                            (complex_id, source, source_id, url, match_method, confidence, matched_by, evidence)
                        VALUES ($1, $2, $3, $4, $5, 1, 'auto', $6::jsonb)
                        ON CONFLICT (source, source_id) DO NOTHING
                    """, cid, source_key, source_id, data.get("url"),
                        "seed_source" if is_new else "ingest_name_exact_or_verified",
                        json.dumps(evidence, ensure_ascii=False, default=str))
                    linked_id = await conn.fetchval(
                        "SELECT complex_id FROM complex_source_links WHERE source=$1 AND source_id=$2",
                        source_key, source_id)
                    if await canonical_complex_id(linked_id, connection=conn) != cid:
                        raise InvalidComplexIdentity(f"Source link changed during import: {source_key}/{source_id}")
                    await record_complex_observations(cid, source_key, source_id, name, data.get("address"),
                                                      evidence=evidence, connection=conn)
                    row = await conn.fetchrow("SELECT source_info FROM complexes WHERE id=$1 FOR UPDATE", cid)
                    existing = {}
                    if row and row["source_info"]:
                        existing = (row["source_info"] if isinstance(row["source_info"], dict)
                                    else json.loads(row["source_info"]))
                    old_data = existing.get(source_key) or {}
                    had_old = source_key in existing
                    existing[source_key] = data
                    if set_housing_class and data.get("housing_class"):
                        await conn.execute("""
                            UPDATE complexes SET housing_class=COALESCE(housing_class,$2),
                                korter_url=COALESCE(korter_url,$3), source_info=$4::jsonb, updated_at=now()
                            WHERE id=$1
                        """, cid, data.get("housing_class"), data.get("url"),
                            json.dumps(existing, ensure_ascii=False, default=str))
                    else:
                        await conn.execute("UPDATE complexes SET source_info=$2::jsonb, updated_at=now() WHERE id=$1",
                                           cid, json.dumps(existing, ensure_ascii=False, default=str))
        except (AmbiguousComplexIdentity, InvalidComplexIdentity) as exc:
            logger.warning("%s identity requires review for %r: %s", source_key, name, exc)
            continue
        matched += 1
        created += int(is_new)

        if is_new:
            change_rows.append((cid, data.get("name") or key, "new", None, None, None))
        elif had_old:
            for f in sorted(set(old_data) | set(data)):
                if f in _SKIP_DIFF_FIELDS:
                    continue
                if _fmt(old_data.get(f)) != _fmt(data.get(f)):
                    change_rows.append((cid, data.get("name") or key, "updated",
                                        f, _fmt(old_data.get(f)), _fmt(data.get(f))))

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
