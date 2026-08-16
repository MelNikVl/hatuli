-- Ретроспективный baseline, продолжение (задача 2026-08-16, "P0 —
-- Integrity") — та же находка, что закрывали migrations/000_baseline_*.sql
-- (55 таблиц, созданных в обход миграций), только на уровень ниже: 23
-- КОЛОНКИ на УЖЕ покрытых миграциями таблицах (apartment_listings/
-- complexes/crime_incidents/housing_class_test), добавленные напрямую на
-- проде и никогда не попавшие ни в одну миграцию. Тот же класс проблемы,
-- что migrations/063_housing_class_estimate_computed_at.sql уже честно
-- признавала для соседней колонки ("housing_class_estimate был заполнен
-- ОДНОРАЗОВЫМ SQL-запуском") — этот файл закрывает остальные найденные
-- случаи в одном заходе, а не по одному при следующем случайном падении.
--
-- Имя файла "039b_..." (не "082_..."): нужно применяться СРАЗУ после того,
-- как появятся последние из родительских таблиц этих колонок —
-- housing_class_test (025) / dedup_scan_log (028) / crime_incidents (039,
-- отсюда "039b") — но РАНЬШЕ migrations/063_housing_class_estimate_
-- computed_at.sql, которая уже читает complexes.housing_class_estimate
-- (UPDATE ... WHERE housing_class_estimate IS NOT NULL) — эмпирически
-- поймано именно на этом: "apply migrations/ с нуля" падало на 063
-- UndefinedColumnError, пока файл ошибочно лежал под номером 082 (после
-- 063, а не до). apartment_listings/complexes/developers — из 000_core_
-- tables.sql, готовы к этому моменту заведомо раньше.
--
-- Полная сверка live information_schema.columns против РЕАЛЬНЫХ
-- DDL-совпадений в migrations/*.sql (не просто упоминание имени колонки
-- где угодно текстом — так, например, complexes.housing_class_estimate
-- ложно казалась "покрытой" только потому, что упоминается в комментарии
-- 063_housing_class_estimate_computed_at.sql, а не в реальном ADD
-- COLUMN) нашла 44 колонки на 8
-- таблицах: apartment_listings/complexes/crime_incidents/housing_class_test
-- (23 колонки, первая волна) + apartment_listings/complexes/crime_incidents/
-- dedup_scan_log/developers/housing_class_test (ещё 21, вторая волна той
-- же сверки со строгим DDL-паттерном).
--
-- ADD COLUMN IF NOT EXISTS — идемпотентно и на пустой БД (создаёт), и на
-- проде (колонки уже есть — no-op). Типы/nullable/default сняты 1:1 с
-- живой БД (information_schema.columns), без блока NOT NULL/UNIQUE, где
-- на проде их и не было.
ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS score_building INTEGER;
ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS is_urgent BOOLEAN;
ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS finish_type TEXT;
ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS floorplan_url TEXT;
ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS floorplan_checked_at TIMESTAMPTZ;

ALTER TABLE complexes ADD COLUMN IF NOT EXISTS kindergarten_distance_m INTEGER;
ALTER TABLE complexes ADD COLUMN IF NOT EXISTS shop_distance_m INTEGER;
ALTER TABLE complexes ADD COLUMN IF NOT EXISTS metro_distance_m INTEGER;
ALTER TABLE complexes ADD COLUMN IF NOT EXISTS score_infrastructure INTEGER;
ALTER TABLE complexes ADD COLUMN IF NOT EXISTS score_developer INTEGER;
ALTER TABLE complexes ADD COLUMN IF NOT EXISTS is_street BOOLEAN DEFAULT false;
ALTER TABLE complexes ADD COLUMN IF NOT EXISTS is_garbage BOOLEAN DEFAULT false;
ALTER TABLE complexes ADD COLUMN IF NOT EXISTS build_status TEXT;
ALTER TABLE complexes ADD COLUMN IF NOT EXISTS deadline TEXT;
ALTER TABLE complexes ADD COLUMN IF NOT EXISTS live_url TEXT;

ALTER TABLE crime_incidents ADD COLUMN IF NOT EXISTS objectid BIGINT;
ALTER TABLE crime_incidents ADD COLUMN IF NOT EXISTS crime_code TEXT;
ALTER TABLE crime_incidents ADD COLUMN IF NOT EXISTS stat TEXT;
ALTER TABLE crime_incidents ADD COLUMN IF NOT EXISTS street TEXT;

ALTER TABLE housing_class_test ADD COLUMN IF NOT EXISTS apartment_count_source TEXT;
ALTER TABLE housing_class_test ADD COLUMN IF NOT EXISTS apartment_count_parsed_at TIMESTAMPTZ;
ALTER TABLE housing_class_test ADD COLUMN IF NOT EXISTS elevator_passenger INTEGER;
ALTER TABLE housing_class_test ADD COLUMN IF NOT EXISTS elevator_freight INTEGER;
ALTER TABLE housing_class_test ADD COLUMN IF NOT EXISTS rooms_1 INTEGER;
ALTER TABLE housing_class_test ADD COLUMN IF NOT EXISTS rooms_2 INTEGER;
ALTER TABLE housing_class_test ADD COLUMN IF NOT EXISTS rooms_3 INTEGER;
ALTER TABLE housing_class_test ADD COLUMN IF NOT EXISTS rooms_4 INTEGER;

-- Вторая волна (строгий DDL-паттерн, не текстовый поиск — см. шапку файла).
ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS comparables_cnt INTEGER;
ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS photo_url TEXT;
ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS developer_id INTEGER;
ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS complex_url TEXT;

ALTER TABLE complexes ADD COLUMN IF NOT EXISTS developer TEXT;
ALTER TABLE complexes ADD COLUMN IF NOT EXISTS score_total REAL;
ALTER TABLE complexes ADD COLUMN IF NOT EXISTS score_location INTEGER;
ALTER TABLE complexes ADD COLUMN IF NOT EXISTS score_quality INTEGER;
ALTER TABLE complexes ADD COLUMN IF NOT EXISTS krisha_url TEXT;
ALTER TABLE complexes ADD COLUMN IF NOT EXISTS photos JSONB;
-- housing_class_estimate — САМА эта колонка (не только _computed_at,
-- которую уже закрыла 063) была "заполнена ОДНОРАЗОВЫМ SQL-запуском"
-- по признанию самой 063 — вот этот ADD COLUMN и есть недостающая часть.
ALTER TABLE complexes ADD COLUMN IF NOT EXISTS housing_class_estimate TEXT;
ALTER TABLE complexes ADD COLUMN IF NOT EXISTS description TEXT;

ALTER TABLE crime_incidents ADD COLUMN IF NOT EXISTS house TEXT;
ALTER TABLE crime_incidents ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'kgp'::text;

ALTER TABLE dedup_scan_log ADD COLUMN IF NOT EXISTS table_name TEXT;
ALTER TABLE dedup_scan_log ADD COLUMN IF NOT EXISTS duplicates_found INTEGER;
-- NOT NULL на пустой таблице (на проде эта миграция — no-op, колонки уже
-- есть) — безопасно только потому, что ADD COLUMN без DEFAULT на СВЕЖУЮ
-- (только что созданную, без единой строки) таблицу задаёт NOT NULL без
-- необходимости бэкафилла существующих строк; на пустой БД dedup_scan_log
-- ещё не наполнена (writer — dedup_scan.py, отдельным прогоном).
ALTER TABLE dedup_scan_log ALTER COLUMN table_name SET NOT NULL;
ALTER TABLE dedup_scan_log ALTER COLUMN duplicates_found SET NOT NULL;

ALTER TABLE developers ADD COLUMN IF NOT EXISTS logo TEXT;
