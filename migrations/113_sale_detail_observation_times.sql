-- Actual source-detail observations only. Legacy details_fetched/last_seen
-- cannot establish when the detail page was checked: leave old rows NULL.
ALTER TABLE apartment_listings
    ADD COLUMN IF NOT EXISTS source_details_checked_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS source_details_attempted_at TIMESTAMPTZ;

COMMENT ON COLUMN apartment_listings.source_details_checked_at IS
    'Successful same-ID, non-archived source detail response; observed fields saved atomically with this timestamp, not a freshness guarantee for absent fields';
COMMENT ON COLUMN apartment_listings.source_details_attempted_at IS
    'Actual started source-detail HTTP attempt, including timeout/block; NULL for cooldown/no request and unknown legacy history';
