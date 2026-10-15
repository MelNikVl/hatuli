-- Journal every transition, including detachment and the preceding ID.
ALTER TABLE listing_complex_resolution_log ALTER COLUMN complex_id DROP NOT NULL;
ALTER TABLE listing_complex_resolution_log
    ADD COLUMN IF NOT EXISTS previous_complex_id INTEGER REFERENCES complexes(id);
ALTER TABLE listing_complex_resolution_log
    ADD COLUMN IF NOT EXISTS previous_resolution_method TEXT;
-- Preserve intentional detachment as a state, so legacy name fallbacks cannot
-- silently add rejected URL/name conflicts back into analytics.
ALTER TABLE apartment_listings DROP CONSTRAINT IF EXISTS apartment_listings_complex_resolution_check;
ALTER TABLE apartment_listings ADD CONSTRAINT apartment_listings_complex_resolution_check
    CHECK (complex_resolution IS NULL OR complex_resolution = ANY (ARRAY['house','url','name','geo','unbound']));
