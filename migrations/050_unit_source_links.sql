-- Фаза 2 (юниты) — spine на уровне newbuild_units.id, тот же паттерн,
-- что complex_source_links (043_entity_resolution.sql), зафиксирован в
-- плане (docs/entity_resolution_plan.md, "Фаза 2 (юниты)") ещё
-- 2026-08-12, реализуется сейчас (гейт п.Б, 2026-08-13).
--
-- Правка задним числом: 049_unit_duplicate_candidates.sql добавил
-- apartment_listings.unit_match_provenance для "слияния" auto-пар — не
-- тот паттерн. apartment_listings — не мусорная дубль-строка, которую
-- можно garbage-флагнуть (в отличие от дубля complexes) — это
-- независимо живущее объявление (аналитика цены/статуса своя), просто
-- ТОЖЕ описывающее тот же физический юнит, что и newbuild_units.
-- entity_id = newbuild_units.id (тот же принцип, что complexes.id у
-- фазы 1: PK без семантики), apartment_listings.id — просто ещё один
-- source ('krisha') в spine, symmetric с complex_source_links.
ALTER TABLE apartment_listings DROP COLUMN IF EXISTS unit_match_provenance;

CREATE TABLE IF NOT EXISTS unit_source_links (
    id            SERIAL PRIMARY KEY,
    unit_id       INT NOT NULL REFERENCES newbuild_units(id) ON DELETE CASCADE,
    source        TEXT NOT NULL,      -- 'krisha' (apartment_listings) — единственный сейчас
    source_id     TEXT NOT NULL,      -- apartment_listings.id
    match_method  TEXT NOT NULL,      -- 'unit_number' | 'floor_area_price' | 'floor_area_dates'
    confidence    NUMERIC,
    evidence      JSONB,
    matched_at    TIMESTAMPTZ DEFAULT now(),
    matched_by    TEXT,
    UNIQUE (source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_unit_source_links_unit ON unit_source_links (unit_id);

GRANT SELECT, INSERT, UPDATE, DELETE ON unit_source_links TO krisha;
GRANT USAGE, SELECT ON SEQUENCE unit_source_links_id_seq TO krisha;
