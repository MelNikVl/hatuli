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


# Мусорные «названия ЖК» — не ЖК по определению
_JUNK_NAMES = {"астана", "город", "жк", "квартира", "дом", "нур-султан",
               "астана город", "казахстан", "продажа", "аренда"}


async def audit_complexes(min_share: float = 0.6, min_cnt: int = 3) -> list[dict]:
    """Возвращает список подозрительных 'ЖК':
    {id, name, listings, street_hits, share, reason}
    Два критерия: 1) название = улица (в адресах объявлений);
    2) мусорное/слишком короткое название («астана», «жк»…)."""
    from bot.db.pg import fetch
    await ensure_columns()
    rows = await fetch("""
        SELECT c.id, c.name, c.is_street, c.krisha_url, c.korter_url,
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
        n = _norm(r["name"])
        # стоп-лист («астана», «жк»…) — мусор всегда, даже с карточкой Крыши
        # (у Крыши бывают псевдо-комплексы с именем города);
        # короткое имя (<4) — мусор, только если нет своей карточки
        junk = (n in _JUNK_NAMES) or \
               (len(n) < 4 and not r["krisha_url"] and not r["korter_url"])
        if share >= min_share or junk:
            out.append({
                "id": r["id"], "name": r["name"],
                "listings": r["listings"], "street_hits": r["street_hits"],
                "share": round(share, 2),
                "reason": "мусорное название" if junk and share < min_share
                          else "название — улица",
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
    # у помеченных улиц убираем координаты — чтобы точно не рисовались на картах
    for s in suspects:
        await execute("UPDATE complexes SET lat = NULL, lon = NULL WHERE id = $1",
                      s["id"])
    # пересчёт координат оставшихся ЖК по актуальным привязкам
    recomputed = await recompute_complex_coords()
    return {"flagged": len(suspects), "unbound": total_unbound,
            "suspects": suspects, "coords_recomputed": recomputed}


async def recompute_complex_coords() -> int:
    """Координаты ЖК = среднее координат его объявлений с ТОЧНЫМИ
    координатами. Правит маркеры 'в реке', которые появились из-за
    кривых привязок. ЖК с карточкой Крыши (krisha_url) не трогаем —
    там координаты официальные."""
    from bot.db.pg import execute
    res = await execute("""
        UPDATE complexes c SET lat = g.lat, lon = g.lon
        FROM (
            SELECT lower(trim(al.complex_name)) AS cx,
                   AVG(al.lat) AS lat, AVG(al.lon) AS lon,
                   COUNT(*) AS n
            FROM apartment_listings al
            WHERE al.complex_name IS NOT NULL AND btrim(al.complex_name) != ''
              AND al.lat IS NOT NULL AND al.lon IS NOT NULL
            GROUP BY 1
        ) g
        WHERE lower(trim(c.name)) = g.cx
          AND g.n >= 2
          AND c.krisha_url IS NULL
          AND COALESCE(c.is_street, FALSE) = FALSE
          AND (c.lat IS NULL
               OR (c.lat - g.lat)^2 + (c.lon - g.lon)^2 > 1.0e-4)  -- дрейф > ~600 м
    """)
    n = int((res or "").split()[-1] or 0)
    logger.info("complex coords recomputed: %d", n)
    return n


async def backfill_year_built() -> int:
    """Год постройки ЖК по данным Korter/Homster почти не покрыт (страницы
    списка не отдают год — нужна отдельная страница ЖК). Зато ~17% объявлений
    с Крыши уже несут year_built на самой карточке квартиры. Берём МОДУ
    (самый частый год) среди объявлений, привязанных к ЖК, и подставляем её
    туда, где у ЖК год ещё не проставлен вручную/из другого источника."""
    from bot.db.pg import execute
    res = await execute("""
        WITH norm AS (
            SELECT lower(trim(regexp_replace(complex_name, '^\\s*(жк|кг)\\.?\\s+', '', 'i'))) AS key,
                   year_built
            FROM apartment_listings
            WHERE year_built IS NOT NULL AND complex_name IS NOT NULL AND is_active IS NOT FALSE
        ),
        modes AS (
            SELECT DISTINCT ON (key) key, year_built
            FROM (
                SELECT key, year_built, count(*) AS cnt
                FROM norm
                GROUP BY key, year_built
            ) g
            ORDER BY key, cnt DESC
        )
        UPDATE complexes c
        SET year_built = m.year_built
        FROM modes m
        WHERE lower(trim(regexp_replace(c.name, '^\\s*(жк|кг)\\.?\\s+', '', 'i'))) = m.key
          AND c.year_built IS NULL
    """)
    n = int((res or "").split()[-1] or 0)
    logger.info("complex year_built backfilled: %d", n)
    return n


async def street_names() -> set[str]:
    """Множество нормализованных названий, помеченных как улицы."""
    from bot.db.pg import fetch
    try:
        rows = await fetch("SELECT name FROM complexes WHERE is_street = TRUE")
    except Exception:
        return set()
    return {_norm(r["name"]) for r in rows if r["name"]}
