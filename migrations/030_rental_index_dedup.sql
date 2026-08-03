-- rental_index accumulated hundreds of duplicate rows per (city, district,
-- complex_name, rooms, prop_type) key whenever any of district/complex_name/
-- rooms was NULL (the "aggregate" fallback levels) - Postgres treats NULL as
-- never-equal in a plain UNIQUE constraint, so ON CONFLICT silently inserted
-- a new row every rebuild instead of updating, and lookup_rental_estimate's
-- unordered LIMIT 1 picked an effectively random historical snapshot (178k
-- rows accumulated over time). Keep only the most recently updated row per
-- key via a window function (fast - a self-join with IS NOT DISTINCT was
-- tried first and was still running after 15+ minutes on 178k rows, causing
-- every service restart to contend for the same migration advisory lock and
-- crash-loop on timeout), then rebuild the constraint with NULLS NOT
-- DISTINCT so ON CONFLICT actually matches going forward.
WITH ranked AS (
  SELECT ctid, ROW_NUMBER() OVER (
    PARTITION BY city, district, complex_name, rooms, prop_type
    ORDER BY updated_at DESC, id DESC
  ) AS rn
  FROM rental_index
)
DELETE FROM rental_index WHERE ctid IN (SELECT ctid FROM ranked WHERE rn > 1);

ALTER TABLE rental_index DROP CONSTRAINT IF EXISTS rental_index_city_district_complex_name_rooms_prop_type_key;
ALTER TABLE rental_index ADD CONSTRAINT rental_index_city_district_complex_name_rooms_prop_type_key
    UNIQUE NULLS NOT DISTINCT (city, district, complex_name, rooms, prop_type);
