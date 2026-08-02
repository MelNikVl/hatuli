-- Планировки квартир (детекция SigLIP)
ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS floorplan_url TEXT;
ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS floorplan_checked_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS listing_floorplans (
  id SERIAL PRIMARY KEY,
  listing_id INTEGER NOT NULL,
  photo_url TEXT,
  floorplan_score REAL,
  other_score REAL,
  is_floorplan BOOLEAN,
  checked_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_listing_fp_listing ON listing_floorplans(listing_id);
GRANT ALL ON listing_floorplans TO krisha;
GRANT ALL ON SEQUENCE listing_floorplans_id_seq TO krisha;
