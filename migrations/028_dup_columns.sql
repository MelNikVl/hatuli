-- dup_match/dup_needs_review/dedup_scan_log already existed on the live DB
-- (created ad-hoc by a runtime ALTER TABLE in terminal_extras.py that ran on
-- every /admin/duplicates page load and started timing out under write load
-- on apartment_listings). Codified here as a proper one-time migration;
-- the runtime DDL call was removed.
ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS dup_match TEXT;
ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS dup_needs_review BOOLEAN DEFAULT FALSE;
CREATE TABLE IF NOT EXISTS dedup_scan_log (
    id SERIAL PRIMARY KEY, table_name TEXT NOT NULL,
    scanned_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    listings_scanned INT NOT NULL, duplicates_found INT NOT NULL,
    needs_review_found INT NOT NULL DEFAULT 0
);
