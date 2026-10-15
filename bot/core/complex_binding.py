"""Привязка объявлений к ЖК/дому по id — apartment_listings.complex_id
(задача 2026-10-03, план месяца п.3; миграция 100).

Порядок (первое сработавшее правило; неуверенно — NULL, не гадаем):
  1. house — resolved_house_id (house_resolution: дом под зонтиком ЖК).
  2. url   — ссылка на ЖК из объявления Крыши (complex_url) совпала по slug с
             ОДНИМ каноническим ЖК (v2, 2026-10-03). Сверка на 7.7K объявлений с
             ссылкой: имя давало тот же ЖК в 98.9%, остальное — уточнение до дома.
  3. name  — complex_name однозначно совпадает с ОДНИМ не-мусорным,
             не-«улица» complexes.name (lower/trim).
  4. geo   — только если имени ЖК в объявлении НЕТ: ближайший дом с
             координатами ≤ GEO_MAX_M, и второй по близости дальше хотя бы
             на GEO_MARGIN_M (иначе неоднозначно). Если имя есть, но не
             совпало ни с одним ЖК, гео не применяем: это другой ЖК, которого
             нет в справочнике, и привязка к соседнему дому была бы ошибкой.

Все правила возвращают КАНОНИЧЕСКИЙ ЖК (complexes.canonical_id, миграция 101,
bot/core/complex_canonical.py): дубли записей одного ЖК сводятся к одной,
обрывки текста без реального ЖК (junk_unmatched) не используются.

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
RESOLVER_VERSION = 'complex_binding_v3'

# Кандидат на каждое объявление; DISTINCT ON + ORDER BY приоритет правила.
_PROPOSE_SQL = f"""
WITH cx AS (
    -- cid — канонический ЖК (миграция 101, bot/core/complex_canonical.py);
    -- обрывки текста без реального ЖК (junk_unmatched) в привязке не участвуют.
    SELECT id, COALESCE(canonical_id, id) AS cid, lower(btrim(name)) AS n, lat, lon,
           lower(substring(krisha_url from '/complex/show/[^/]+/([^/?#]+)')) AS slug
      FROM complexes
     WHERE coalesce(is_garbage, false) = false AND coalesce(is_street, false) = false
       AND coalesce(canonical_reason, '') <> 'junk_unmatched'
),
uniq AS (SELECT n, min(cid) AS id FROM cx WHERE n <> '' GROUP BY n HAVING count(DISTINCT cid) = 1),
-- без HAVING: планировщик оценивает HAVING в 1 строку и уходит в nested loop с
-- полным сканом объявлений на каждый slug; условие k = 1 — в JOIN.
slugs AS (SELECT slug, min(cid) AS id, count(DISTINCT cid) AS k FROM cx WHERE slug IS NOT NULL GROUP BY slug),
house AS (
    SELECT a.id AS listing_id, cx.cid AS complex_id, 'house' AS method, 1 AS prio,
           jsonb_build_object('resolved_house_id', a.resolved_house_id) AS evidence
      FROM apartment_listings a JOIN cx ON cx.id = a.resolved_house_id
     WHERE a.resolved_house_id IS NOT NULL {{scope}}
),
lurl AS MATERIALIZED (
    SELECT a.id, a.complex_url, lower(substring(a.complex_url from '/complex/show/[^/]+/([^/?#]+)')) AS slug
      FROM apartment_listings a
     WHERE nullif(a.complex_url, '') IS NOT NULL {{scope}}
),
byurl AS (
    -- ссылка на ЖК из самого объявления Крыши — самый надёжный ключ после дома под зонтиком
    SELECT l.id, s.id, 'url', 2, jsonb_build_object('complex_url', l.complex_url)
      FROM lurl l JOIN slugs s ON s.k = 1 AND s.slug = l.slug
),
byname AS (
    SELECT a.id, u.id, 'name', 3, jsonb_build_object('complex_name', a.complex_name)
      FROM apartment_listings a JOIN uniq u ON u.n = lower(btrim(a.complex_name))
     WHERE nullif(btrim(a.complex_name), '') IS NOT NULL {{scope}}
),
geo AS (
    SELECT a.id, g.c1, 'geo', 4, jsonb_build_object('dist_m', round(g.d1::numeric, 1), 'second_m', round(g.d2::numeric, 1))
      FROM apartment_listings a
      CROSS JOIN LATERAL (
          SELECT (array_agg(cid ORDER BY d))[1] AS c1, (array_agg(d ORDER BY d))[1] AS d1, (array_agg(d ORDER BY d))[2] AS d2
            FROM (SELECT cx.cid, min(sqrt(((cx.lat - a.lat) * 111000)^2 + ((cx.lon - a.lon) * 111000 * cos(radians(a.lat)))^2)) AS d
                    FROM cx
                   WHERE cx.lat BETWEEN a.lat - 0.0015 AND a.lat + 0.0015
                     AND cx.lon BETWEEN a.lon - 0.0025 AND a.lon + 0.0025
                   GROUP BY cx.cid) near
      ) g
     WHERE nullif(btrim(a.complex_name), '') IS NULL AND a.lat IS NOT NULL
       AND g.d1 <= {GEO_MAX_M} AND (g.d2 IS NULL OR g.d2 - g.d1 >= {GEO_MARGIN_M}) {{scope}}
),
prop AS (
    SELECT DISTINCT ON (listing_id) listing_id, complex_id, method, evidence
      FROM (SELECT * FROM house UNION ALL SELECT * FROM byurl UNION ALL SELECT * FROM byname UNION ALL SELECT * FROM geo) u
     ORDER BY listing_id, prio
),
target AS (
    SELECT a.id AS listing_id, p.complex_id, p.method, p.evidence, a.complex_name,
           a.complex_id AS previous_complex_id, a.complex_resolution AS previous_complex_resolution
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
       count(*) FILTER (WHERE complex_resolution = 'url') AS by_url,
       count(*) FILTER (WHERE complex_resolution = 'name') AS by_name,
       count(*) FILTER (WHERE complex_resolution = 'geo') AS by_geo
  FROM apartment_listings
 WHERE is_active IS NOT FALSE AND coalesce(is_duplicate, false) = false
"""


def _scope_sql(active_only: bool, id_like: str | None = None) -> str:
    scope = "AND a.is_active IS NOT FALSE" if active_only else ""
    if id_like is not None:
        scope += " AND a.id LIKE $1"   # только для тестов: не трогать боевые строки
    return scope


def build_sql(active_only: bool, id_like: str | None = None) -> str:
    return _APPLY_SQL.replace('{scope}', _scope_sql(active_only, id_like))


def build_preview_sql(active_only: bool = False, id_like: str | None = None) -> str:
    """Read-only proposal using the CURRENT canonical map; no UPDATE or log writes."""
    return _PROPOSE_SQL.replace('{scope}', _scope_sql(active_only, id_like)) + """
SELECT listing_id, previous_complex_id, previous_complex_resolution,
       complex_id, method, evidence
  FROM target ORDER BY listing_id
"""


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
