-- Persist the outcome of floor enrichment without inventing a floor.
-- Only `not_applicable` (a flat-layout card, not a physical apartment) is
-- excluded from future floor-backfill selection; other outcomes remain
-- retriable because an archived listing can reappear or source markup change.
ALTER TABLE apartment_listings
    ADD COLUMN IF NOT EXISTS floor_backfill_outcome TEXT,
    ADD COLUMN IF NOT EXISTS floor_backfill_checked_at TIMESTAMPTZ;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'apartment_listings_floor_backfill_outcome_check'
          AND conrelid = 'apartment_listings'::regclass
    ) THEN
        ALTER TABLE apartment_listings
            ADD CONSTRAINT apartment_listings_floor_backfill_outcome_check
            CHECK (floor_backfill_outcome IS NULL OR floor_backfill_outcome IN
                   ('floor_filled', 'floor_not_found', 'unavailable', 'not_applicable', 'blocked', 'error'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_apartment_listings_floor_backfill_pending
    ON apartment_listings (floor_backfill_outcome)
    WHERE floor IS NULL;
