-- Карантин координат (задача 2026-08-12, см. docs/entity_resolution_plan.md
-- — аудит blob-комплексов нашёл 2 явно битых значения: Dream City
-- (homeportal-объект в 899 км от остальных точек того же ЖК — Туркестан
-- вместо Астаны), GreenLine. Headliner Exclusive (474 км — Караганда).
-- Ни один парсер, пишущий гео (hype_tracker/homeportal_scan.py,
-- newbuild_common.py), не валидировал координаты на входе — bbox-проверка
-- была только в krisha_complex_import.py.
--
-- Карантин на уровне ЗНАЧЕНИЯ (не строки целиком) — обнулили конкретное
-- битое latitude/longitude, обосновали причину, не трогая остальные
-- поля той же строки.
ALTER TABLE homeportal_objects ADD COLUMN IF NOT EXISTS geo_quarantined_at TIMESTAMPTZ;
ALTER TABLE homeportal_objects ADD COLUMN IF NOT EXISTS geo_quarantine_reason TEXT;
-- Исходные (до обнуления) значения — на случай, если карантин окажется
-- слишком агрессивным и придётся откатить вручную.
ALTER TABLE homeportal_objects ADD COLUMN IF NOT EXISTS geo_quarantined_lat TEXT;
ALTER TABLE homeportal_objects ADD COLUMN IF NOT EXISTS geo_quarantined_lon TEXT;

ALTER TABLE complexes ADD COLUMN IF NOT EXISTS geo_quarantined_at TIMESTAMPTZ;
ALTER TABLE complexes ADD COLUMN IF NOT EXISTS geo_quarantine_reason TEXT;
ALTER TABLE complexes ADD COLUMN IF NOT EXISTS geo_quarantined_lat DOUBLE PRECISION;
ALTER TABLE complexes ADD COLUMN IF NOT EXISTS geo_quarantined_lon DOUBLE PRECISION;

CREATE INDEX IF NOT EXISTS idx_homeportal_geo_quarantined ON homeportal_objects (geo_quarantined_at) WHERE geo_quarantined_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_complexes_geo_quarantined ON complexes (geo_quarantined_at) WHERE geo_quarantined_at IS NOT NULL;
