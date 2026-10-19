-- Derived asking-rent scenarios. No contract rents, tax status or demand labels.
ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS rental_economics JSONB;
CREATE INDEX IF NOT EXISTS idx_apt_net_yield_active ON apartment_listings(net_yield_pct DESC) WHERE is_active;
CREATE TABLE IF NOT EXISTS rental_yield_benchmarks (
    scope TEXT NOT NULL CHECK(scope IN ('city','district','class','district_class')),
    scope_value TEXT, rooms INTEGER NOT NULL CHECK(rooms BETWEEN 1 AND 6),
    median_net DOUBLE PRECISION NOT NULL, p90_net DOUBLE PRECISION NOT NULL,
    n INTEGER NOT NULL CHECK(n>=10), net_values DOUBLE PRECISION[] NOT NULL,
    computed_at TIMESTAMPTZ NOT NULL, source_oldest_at TIMESTAMPTZ NOT NULL,
    source_newest_at TIMESTAMPTZ NOT NULL, params_used JSONB NOT NULL,
    policy_version TEXT NOT NULL,
    CONSTRAINT rental_yield_benchmarks_key UNIQUE NULLS NOT DISTINCT(scope,scope_value,rooms)
);
-- Extend the existing saved-search and notification ledger, rather than
-- introducing another subscription store or another Telegram sender.
ALTER TABLE saved_searches
    ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'listing',
    ADD COLUMN IF NOT EXISTS params JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS enabled BOOLEAN NOT NULL DEFAULT TRUE;
CREATE UNIQUE INDEX IF NOT EXISTS idx_saved_search_typed_user
    ON saved_searches(user_id,kind) WHERE kind IN ('yield','sublease');
ALTER TABLE user_listing_notifications
    ADD COLUMN IF NOT EXISTS subscription_id INTEGER REFERENCES saved_searches(id),
    ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'listing',
    ADD COLUMN IF NOT EXISTS property_id BIGINT,
    ADD COLUMN IF NOT EXISTS payload JSONB,
    ADD COLUMN IF NOT EXISTS delivery_status TEXT NOT NULL DEFAULT 'sent',
    ADD COLUMN IF NOT EXISTS attempted_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMPTZ;
CREATE UNIQUE INDEX IF NOT EXISTS idx_notifications_typed_property
    ON user_listing_notifications(user_id,kind,property_id)
    WHERE kind IN ('yield','sublease') AND property_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_notifications_pending
    ON user_listing_notifications(notified_at) WHERE delivery_status='pending';
CREATE INDEX IF NOT EXISTS idx_notifications_daily_cap
    ON user_listing_notifications(user_id,notified_at) WHERE kind IN ('yield','sublease');
CREATE INDEX IF NOT EXISTS idx_notifications_delivery_cap
    ON user_listing_notifications(user_id,attempted_at) WHERE kind IN ('yield','sublease');
