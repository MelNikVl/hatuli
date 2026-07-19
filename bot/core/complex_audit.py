"""
Аудит справочника ЖК: отлов «псевдо-ЖК», которые на самом деле улицы.

Откуда они берутся: у Крыши блок «Жилой комплекс» на странице объявления
для старых домов часто заполнен НАЗВАНИЕМ УЛИЦЫ (krisha группирует такие
дома в pseudo-complex), а наш текстовый rebind дополнительно склеивал
объявления по вхождению названия в адрес. Итог: «ЖК Бухар Жырау», под
которым собраны случайные квартиры со всей улицы.

Критерий улицы: у ≥60% (и минимум у 3) привязанных объявлений название
«ЖК» встречается в строке АДРЕСА объявления (нормализовано, без «жк»).
У настоящего ЖК адреса квартир лежат на ДРУГИХ улицах (как у настоящего
«Бухар Жырау» от ULYTAU — адрес ЖК: ул. Туран 50/3-5).

Помеченные улицы НЕ удаляются (аудит-трейл), а флагаются is_street=TRUE:
- исключаются из карты ЖК, rebind, каталога (кроме пометки);
- их объявления отвязываются (complex_name/complex_url → NULL);
- парсер больше не примет это название как ЖК (проверка на входе).
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


def _norm(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"^\s*(жк|кг)\.?\s+", "", s)
    s = re.sub(r"[«»\"'()]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


async def ensure_columns() -> None:
    from bot.db.pg import execute
    await execute("ALTER TABLE complexes ADD COLUMN IF NOT EXISTS is_street BOOLEAN DEFAULT FALSE")


async def audit_complexes(min_share: float = 0.6, min_cnt: int = 3) -> list[dict]:
    """Возвращает список подозрительных 'ЖК':
    {id, name, listings, street_hits, share}"""
    from bot.db.pg import fetch
    await ensure_columns()
    rows = await fetch("""
        SELECT c.id, c.name, c.is_street,
               COUNT(al.id) AS listings,
               COUNT(al.id) FILTER (
                   WHERE al.address IS NOT NULL
                     AND position(lower(trim(regexp_replace(c.name, '^\\s*(жк|кг)\\.?\\s+', '', 'i')))
                                  in lower(al.address)) > 0
               ) AS street_hits
        FROM complexes c
        JOIN apartment_listings al
          ON lower(trim(al.complex_name)) = lower(trim(c.name))
        WHERE COALESCE(c.is_street, FALSE) = FALSE
        GROUP BY c.id, c.name, c.is_street
        HAVING COUNT(al.id) >= $1
        ORDER BY listings DESC
    """, min_cnt)
    out = []
    for r in rows:
        share = (r["street_hits"] or 0) / r["listings"]
        if share >= min_share:
            out.append({
                "id": r["id"], "name": r["name"],
                "listings": r["listings"], "street_hits": r["street_hits"],
                "share": round(share, 2),
            })
    return out


async def purge_street_complexes(min_share: float = 0.6, min_cnt: int = 3) -> dict:
    """Флагает псевдо-ЖК как улицы и отвязывает их объявления."""
    from bot.db.pg import execute, fetchval
    suspects = await audit_complexes(min_share, min_cnt)
    total_unbound = 0
    for s in suspects:
        n = await fetchval("""
            WITH upd AS (
                UPDATE apartment_listings
                SET complex_name = NULL, complex_url = NULL
                WHERE lower(trim(complex_name)) = lower(trim($1))
                RETURNING id)
            SELECT COUNT(*) FROM upd
        """, s["name"]) or 0
        await execute(
            "UPDATE complexes SET is_street = TRUE, updated_at = now() WHERE id = $1",
            s["id"])
        total_unbound += n
        logger.warning("audit: '%s' помечен улицей (%.0f%% адресов), отвязано %d объявлений",
                       s["name"], s["share"] * 100, n)
    return {"flagged": len(suspects), "unbound": total_unbound, "suspects": suspects}


async def street_names() -> set[str]:
    """Множество нормализованных названий, помеченных как улицы."""
    from bot.db.pg import fetch
    try:
        rows = await fetch("SELECT name FROM complexes WHERE is_street = TRUE")
    except Exception:
        return set()
    return {_norm(r["name"]) for r in rows if r["name"]}
