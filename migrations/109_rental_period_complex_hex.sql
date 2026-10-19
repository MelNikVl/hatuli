-- Unknown legacy periods stay unknown: an amount cannot establish a price unit.
ALTER TABLE rental_listings
    ADD COLUMN IF NOT EXISTS rent_period TEXT NOT NULL DEFAULT 'unknown',
    ADD COLUMN IF NOT EXISTS rent_period_conflict BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS complex_id INTEGER REFERENCES complexes(id),
    ADD COLUMN IF NOT EXISTS hex_id TEXT,
    ADD COLUMN IF NOT EXISTS hex_edge_m REAL;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='rental_listings'::regclass AND conname='rental_listings_rent_period_check') THEN
        ALTER TABLE rental_listings ADD CONSTRAINT rental_listings_rent_period_check
            CHECK (rent_period IN ('daily','monthly','unknown'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='rental_listings'::regclass AND conname='rental_listings_hex_edge_check') THEN
        ALTER TABLE rental_listings ADD CONSTRAINT rental_listings_hex_edge_check
            CHECK ((hex_id IS NULL AND hex_edge_m IS NULL) OR (hex_id IS NOT NULL AND hex_edge_m IS NOT NULL AND hex_id ~ '^-?[0-9]+:-?[0-9]+$' AND hex_edge_m BETWEEN 10 AND 1000));
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_rental_period_active ON rental_listings(rent_period) WHERE is_active;
CREATE INDEX IF NOT EXISTS idx_rental_complex_id ON rental_listings(complex_id);
CREATE INDEX IF NOT EXISTS idx_rental_hex ON rental_listings(hex_edge_m,hex_id);
