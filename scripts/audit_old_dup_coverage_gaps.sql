-- Read-only audit, Stage 1.2 ("Property Identity — review calibration",
-- 2026-08-18): почему часть старых apartment_listings.is_duplicate=TRUE
-- связей НЕ представлена ни одной строкой (любого статуса) в
-- property_match_candidates. Ничего не пишет, кандидатов не создаёт.

\echo '=== 0. Пересчёт после деплоя (сверка с прошлым аудитом: было 15830/9179/14066/1764) ==='
SELECT count(*) FROM apartment_listings WHERE is_duplicate = TRUE;
SELECT count(DISTINCT duplicate_of) FROM apartment_listings WHERE is_duplicate = TRUE AND duplicate_of IS NOT NULL;

-- complex_id НЕ хранится буквально на apartment_listings — резолвится
-- по имени через complexes (та же логика, что property_linker.py::
-- _resolve_complex_id: lower(trim(name)) lookup), повторяем её здесь.
DROP TABLE IF EXISTS _old_dups;
CREATE TEMP TABLE _old_dups AS
SELECT od.id AS listing_id, od.duplicate_of,
       al_a.floor AS floor_a, al_a.area AS area_a, al_a.address AS address_a,
       cx_a.id AS complex_id_a,
       al_a.is_active AS active_a, al_a.archived_at AS archived_a,
       al_b.id AS other_exists, al_b.floor AS floor_b, al_b.area AS area_b, al_b.address AS address_b,
       cx_b.id AS complex_id_b,
       al_b.is_active AS active_b, al_b.archived_at AS archived_b,
       pl_a.property_id AS prop_a, pl_b.property_id AS prop_b
FROM apartment_listings od
JOIN apartment_listings al_a ON al_a.id = od.id
LEFT JOIN apartment_listings al_b ON al_b.id = od.duplicate_of
LEFT JOIN property_listings pl_a ON pl_a.listing_id = od.id
LEFT JOIN property_listings pl_b ON pl_b.listing_id = od.duplicate_of
LEFT JOIN complexes cx_a ON lower(trim(cx_a.name)) = lower(trim(al_a.complex_name))
LEFT JOIN complexes cx_b ON lower(trim(cx_b.name)) = lower(trim(al_b.complex_name))
WHERE od.is_duplicate = TRUE AND od.duplicate_of IS NOT NULL;

\echo '=== 1. Not represented cohort size (должно совпасть с прошлым аудитом: 1764) ==='
DROP TABLE IF EXISTS _not_represented;
CREATE TEMP TABLE _not_represented AS
SELECT * FROM _old_dups od
WHERE NOT (prop_a IS NOT NULL AND prop_b IS NOT NULL AND prop_a = prop_b)
  AND NOT EXISTS (
    SELECT 1 FROM property_match_candidates pmc
    WHERE (pmc.listing_id = od.listing_id AND pmc.candidate_property_id = od.prop_b)
       OR (pmc.listing_id = od.duplicate_of AND pmc.candidate_property_id = od.prop_a)
  );
SELECT count(*) FROM _not_represented;

\echo '=== 2. Причина: duplicate_of ссылается на несуществующий listing (сломанная ссылка) ==='
SELECT count(*) FROM _not_represented WHERE other_exists IS NULL;

\echo '=== 3. Причина: отсутствует property_id хотя бы с одной стороны (среди оставшихся, other_exists не NULL) ==='
SELECT count(*) FROM _not_represented WHERE other_exists IS NOT NULL AND (prop_a IS NULL OR prop_b IS NULL);
SELECT
    count(*) FILTER (WHERE prop_a IS NULL AND prop_b IS NOT NULL) AS only_a_missing,
    count(*) FILTER (WHERE prop_b IS NULL AND prop_a IS NOT NULL) AS only_b_missing,
    count(*) FILTER (WHERE prop_a IS NULL AND prop_b IS NULL) AS both_missing
FROM _not_represented WHERE other_exists IS NOT NULL;

\echo '=== 4. Из "both have property_id, no candidate row" (2 IS NOT NULL) — отсутствует этаж хотя бы с одной стороны ==='
SELECT count(*) FROM _not_represented
WHERE other_exists IS NOT NULL AND prop_a IS NOT NULL AND prop_b IS NOT NULL
  AND (floor_a IS NULL OR floor_b IS NULL);

\echo '=== 5. ...отсутствует площадь хотя бы с одной стороны ==='
SELECT count(*) FROM _not_represented
WHERE other_exists IS NOT NULL AND prop_a IS NOT NULL AND prop_b IS NOT NULL
  AND (area_a IS NULL OR area_b IS NULL);

\echo '=== 6. ...оба поля (этаж+площадь) присутствуют, но разные complex_id (fuzzy требует тот же complex_id) ==='
SELECT count(*) FROM _not_represented
WHERE other_exists IS NOT NULL AND prop_a IS NOT NULL AND prop_b IS NOT NULL
  AND floor_a IS NOT NULL AND floor_b IS NOT NULL AND area_a IS NOT NULL AND area_b IS NOT NULL
  AND complex_id_a IS DISTINCT FROM complex_id_b;

\echo '=== 7. ...оба поля присутствуют, тот же complex_id, но всё равно нет кандидата (нужен ручной разбор — вероятно fuzzy score < порога/address не совпал) ==='
SELECT count(*) FROM _not_represented
WHERE other_exists IS NOT NULL AND prop_a IS NOT NULL AND prop_b IS NOT NULL
  AND floor_a IS NOT NULL AND floor_b IS NOT NULL AND area_a IS NOT NULL AND area_b IS NOT NULL
  AND complex_id_a IS NOT DISTINCT FROM complex_id_b;

\echo '=== 8. both_missing property_id -> оба листинга вообще не забутстрапены (Property Identity ещё не обработал) ==='
SELECT count(*) FROM _not_represented
WHERE other_exists IS NOT NULL AND prop_a IS NULL AND prop_b IS NULL;

\echo '=== 9. Примеры каждой причины (обезличенные — только id/floor/area/complex_id, без адреса/цены/продавца) ==='
\echo '--- 9a. сломанная ссылка duplicate_of (5 примеров) ---'
SELECT listing_id, duplicate_of FROM _not_represented WHERE other_exists IS NULL LIMIT 5;

\echo '--- 9b. отсутствует property_id (5 примеров) ---'
SELECT listing_id, duplicate_of, prop_a, prop_b FROM _not_represented
WHERE other_exists IS NOT NULL AND (prop_a IS NULL OR prop_b IS NULL) LIMIT 5;

\echo '--- 9c. отсутствует этаж (5 примеров) ---'
SELECT listing_id, duplicate_of, floor_a, floor_b FROM _not_represented
WHERE other_exists IS NOT NULL AND prop_a IS NOT NULL AND prop_b IS NOT NULL
  AND (floor_a IS NULL OR floor_b IS NULL) LIMIT 5;

\echo '--- 9d. разные complex_id (5 примеров) ---'
SELECT listing_id, duplicate_of, complex_id_a, complex_id_b FROM _not_represented
WHERE other_exists IS NOT NULL AND prop_a IS NOT NULL AND prop_b IS NOT NULL
  AND floor_a IS NOT NULL AND floor_b IS NOT NULL AND area_a IS NOT NULL AND area_b IS NOT NULL
  AND complex_id_a IS DISTINCT FROM complex_id_b LIMIT 5;

\echo '--- 9e. "неясная" (все поля есть, тот же complex_id, но нет кандидата) — 8 примеров для ручного разбора ---'
SELECT listing_id, duplicate_of, floor_a, floor_b, area_a, area_b, complex_id_a FROM _not_represented
WHERE other_exists IS NOT NULL AND prop_a IS NOT NULL AND prop_b IS NOT NULL
  AND floor_a IS NOT NULL AND floor_b IS NOT NULL AND area_a IS NOT NULL AND area_b IS NOT NULL
  AND complex_id_a IS NOT DISTINCT FROM complex_id_b LIMIT 8;
