-- Property Identity, слой 2 (задача 2026-08-16, "P1 — Property Identity")
-- — связь properties (физическая квартира) <-> apartment_listings
-- (конкретное объявление под ней когда-либо) — many-to-one: одна
-- квартира может иметь много listing_id (relist'ы), но UNIQUE(listing_id)
-- ниже гарантирует обратное: один listing_id — ровно одна квартира,
-- никогда не двусмысленно.
--
-- link_method — как связь установлена (пишет bot/identity/property_
-- linker.py::link_listing_to_property()):
--   'auto'   — точное совпадение address_hash (либо новая квартира создана
--              под этим объявлением впервые).
--   'fuzzy'  — совпадение по complex_id+floor+area_sqm (±1м² допуск), сам
--              address_hash НЕ совпал (опечатка/другое написание адреса)
--              — confidence < 1.0.
--   'manual' — ручная правка (не пишется линковщиком, задел на будущее,
--              тот же паттерн, что matched_by='manual' в
--              complex_source_links).
--
-- confidence — 1.0 у 'auto', < 1.0 у 'fuzzy' (формула — property_linker.py,
-- не дублируется здесь).
CREATE TABLE IF NOT EXISTS property_listings (
    id          SERIAL PRIMARY KEY,
    property_id INTEGER NOT NULL REFERENCES properties(property_id) ON DELETE CASCADE,
    listing_id  TEXT NOT NULL REFERENCES apartment_listings(id) ON DELETE CASCADE,
    linked_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    link_method TEXT NOT NULL CHECK (link_method = ANY (ARRAY['auto', 'manual', 'fuzzy'])),
    confidence  NUMERIC NOT NULL DEFAULT 1.0,
    UNIQUE (listing_id)
);

CREATE INDEX IF NOT EXISTS idx_property_listings_property ON property_listings (property_id);

GRANT SELECT, INSERT, UPDATE, DELETE ON property_listings TO krisha;
GRANT USAGE, SELECT ON SEQUENCE property_listings_id_seq TO krisha;
