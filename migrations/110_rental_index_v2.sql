-- The new builder supplies the period and provenance explicitly. Existing
-- aggregates receive no policy version and cannot pass the v3 reader gate.
ALTER TABLE rental_index
    ADD COLUMN IF NOT EXISTS rent_period TEXT NOT NULL DEFAULT 'unknown',
    ADD COLUMN IF NOT EXISTS complex_id INTEGER REFERENCES complexes(id),
    ADD COLUMN IF NOT EXISTS furnishing TEXT,
    ADD COLUMN IF NOT EXISTS window_days INTEGER,
    ADD COLUMN IF NOT EXISTS median_dom_days REAL,
    ADD COLUMN IF NOT EXISTS gone_30d INTEGER,
    ADD COLUMN IF NOT EXISTS active_supply INTEGER,
    ADD COLUMN IF NOT EXISTS source_oldest_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS source_newest_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS policy_version TEXT;
ALTER TABLE rental_index DROP CONSTRAINT IF EXISTS rental_index_city_district_complex_name_rooms_prop_type_key;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='rental_index'::regclass AND conname='rental_index_key_v2') THEN
        ALTER TABLE rental_index ADD CONSTRAINT rental_index_key_v2
            UNIQUE NULLS NOT DISTINCT(city,district,complex_name,complex_id,rooms,prop_type,rent_period,furnishing);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='rental_index'::regclass AND conname='rental_index_rent_period_check') THEN
        ALTER TABLE rental_index ADD CONSTRAINT rental_index_rent_period_check
            CHECK (rent_period IN ('daily','monthly','unknown'));
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_rental_index_period_complex ON rental_index(city,rent_period,complex_id,rooms);
