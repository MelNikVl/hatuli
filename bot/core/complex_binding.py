"""Привязка объявлений к ЖК/дому по id — apartment_listings.complex_id
(задача 2026-10-03, план месяца п.3; миграция 100).

Порядок (первое сработавшее правило; неуверенно — NULL, не гадаем):
  1. house — resolved_house_id (house_resolution: дом под зонтиком ЖК).
  2. name  — complex_name однозначно совпадает с ОДНИМ не-мусорным,
             не-«улица» complexes.name (lower/trim).
  3. geo   — только если имени ЖК в объявлении НЕТ: ближайший дом с
             координатами ≤ GEO_MAX_M, и второй по близости дальше хотя бы
             на GEO_MARGIN_M (иначе неоднозначно). Если имя есть, но не
             совпало ни с одним ЖК, гео не применяем: это другой ЖК, которого
             нет в справочнике, и привязка к соседнему дому была бы ошибкой.

complex_name не трогается — им владеет rebind (service_geobind.py).
Каждое изменение complex_id — строка в listing_complex_resolution_log
(append-only журнал, миграция 096): tier A для house/name, B для geo.
Идемпотентно: повторный прогон без изменений данных ничего не пишет.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

GEO_MAX_M = 60
GEO_MARGIN_M = 15
RESOLVER_VERSION = 'complex_binding_v1'

# Кандидат на каждое объявление; DISTINCT ON + ORDER BY приоритет правила.
_PROPOSE_SQL = f"""
WITH cx AS (
    SELECT id, lower(btrim(name)) AS n, lat, lon
      FROM complexes
     WHERE coalesce(is_garbage, false) = false AND coalesce(is_street, false) = false
),
uniq AS (SELECT n, min(id) AS id FROM cx WHERE n <> '' GROUP BY n HAVING count(*) = 1),
house AS (
    SELECT a.id AS listing_id, a.resolved_house_id AS complex_id, 'house' AS method, 1 AS prio,
           jsonb_build_object('resolved_house_id', a.resolved_house_id) AS evidence
      FROM apartment_listings a JOIN cx ON cx.id = a.resolved_house_id
     WHERE a.resolved_house_id IS NOT NULL {{scope}}
),
byname AS (
    SELECT a.id, u.id, 'name', 2, jsonb_build_object('complex_name', a.complex_name)
      FROM apartment_listings a JOIN uniq u ON u.n = lower(btrim(a.complex_name))
     WHERE nullif(btrim(a.complex_name), '') IS NOT NULL {{scope}}
),
geo AS (
    SELECT a.id, g.c1, 'geo', 3, jsonb_build_object('dist_m', round(g.d1::numeric, 1), 'second_m', round(g.d2::numeric, 1))
      FROM apartment_listings a
      CROSS JOIN LATERAL (
          SELECT (array_agg(id ORDER BY d))[1] AS c1, (array_agg(d ORDER BY d))[1] AS d1, (array_agg(d ORDER BY d))[2] AS d2
            FROM (SELECT cx.id, sqrt(((cx.lat - a.lat) * 111000)^2 + ((cx.lon - a.lon) * 111000 * cos(radians(a.lat)))^2) AS d
                    FROM cx
                   WHERE cx.lat BETWEEN a.lat - 0.0015 AND a.lat + 0.0015
                     AND cx.lon BETWEEN a.lon - 0.0025 AND a.lon + 0.0025) near
      ) g
     WHERE nullif(btrim(a.complex_name), '') IS NULL AND a.lat IS NOT NULL
       AND g.d1 <= {GEO_MAX_M} AND (g.d2 IS NULL OR g.d2 - g.d1 >= {GEO_MARGIN_M}) {{scope}}
),
prop AS (
    SELECT DISTINCT ON (listing_id) listing_id, complex_id, method, evidence
      FROM (SELECT * FROM house UNION ALL SELECT * FROM byname UNION ALL SELECT * FROM geo) u
     ORDER BY listing_id, prio
),
target AS (
    SELECT a.id AS listing_id, p.complex_id, p.method, p.evidence, a.complex_name
      FROM apartment_listings a LEFT JOIN prop p ON p.listing_id = a.id
     WHERE (a.complex_id IS DISTINCT FROM p.complex_id OR a.complex_resolution IS DISTINCT FROM p.method) {{scope}}
)
"""

_APPLY_SQL = _PROPOSE_SQL + """,
upd AS (
    UPDATE apartment_listings a SET complex_id = t.complex_id, complex_resolution = t.method
      FROM target t WHERE a.id = t.listing_id
    RETURNING a.id
),
logged AS (
    INSERT INTO listing_complex_resolution_log
        (listing_id, complex_id, resolution_method, confidence_tier, resolved_at,
         complex_name_at_resolution, evidence, resolver_version)
    SELECT t.listing_id, t.complex_id, t.method, CASE WHEN t.method = 'geo' THEN 'B' ELSE 'A' END, now(),
           t.complex_name, t.evidence, '{ver}'
      FROM target t JOIN upd ON upd.id = t.listing_id
     WHERE t.complex_id IS NOT NULL
    RETURNING 1
)
SELECT (SELECT count(*) FROM upd) AS changed, (SELECT count(*) FROM logged) AS logged
""".replace('{ver}', RESOLVER_VERSION)

_COVERAGE_SQL = """
SELECT count(*) AS total,
       count(complex_id) AS bound,
       count(*) FILTER (WHERE complex_resolution = 'house') AS by_house,
       count(*) FILTER (WHERE complex_resolution = 'name') AS by_name,
       count(*) FILTER (WHERE complex_resolution = 'geo') AS by_geo
  FROM apartment_listings
 WHERE is_active IS NOT FALSE AND coalesce(is_duplicate, false) = false
"""


def build_sql(active_only: bool, id_like: str | None = None) -> str:
    scope = "AND a.is_active IS NOT FALSE" if active_only else ""
    if id_like is not None:
        scope += " AND a.id LIKE $1"   # только для тестов: не трогать боевые строки
    return _APPLY_SQL.replace('{scope}', scope)


async def resolve_complex_ids(*, active_only: bool = False, id_like: str | None = None) -> dict:
    from bot.db.pg import fetchrow
    sql = build_sql(active_only, id_like)
    row = await (fetchrow(sql, id_like) if id_like is not None else fetchrow(sql))
    res = {'changed': int(row['changed']), 'logged': int(row['logged'])}
    logger.info('complex_binding: changed=%(changed)d logged=%(logged)d', res)
    return res


async def coverage() -> dict:
    from bot.db.pg import fetchrow
    r = dict(await fetchrow(_COVERAGE_SQL))
    r['bound_pct'] = round(100.0 * r['bound'] / r['total'], 1) if r['total'] else 0.0
    return r
