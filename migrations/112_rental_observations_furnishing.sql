-- Stage 2 accumulates genuine source observations; no legacy snapshot backfill.
ALTER TABLE rental_listings
    ADD COLUMN IF NOT EXISTS description TEXT,
    ADD COLUMN IF NOT EXISTS description_checked_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS furnishing TEXT NOT NULL DEFAULT 'unknown',
    ADD COLUMN IF NOT EXISTS furnishing_version TEXT,
    ADD COLUMN IF NOT EXISTS rental_episode_started_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS rental_episode_last_seen_at TIMESTAMPTZ;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='rental_listings'::regclass AND conname='rental_listings_furnishing_check') THEN
        ALTER TABLE rental_listings ADD CONSTRAINT rental_listings_furnishing_check
            CHECK(furnishing IN ('none','partial','full','unknown'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='rental_listings'::regclass AND conname='rental_listings_episode_check') THEN
        ALTER TABLE rental_listings ADD CONSTRAINT rental_listings_episode_check
            CHECK((rental_episode_started_at IS NULL AND rental_episode_last_seen_at IS NULL) OR
                  (rental_episode_started_at IS NOT NULL AND rental_episode_last_seen_at IS NOT NULL
                   AND rental_episode_started_at<=rental_episode_last_seen_at));
    END IF;
END $$;
ALTER TABLE rental_index ADD COLUMN IF NOT EXISTS months_of_supply REAL;
ALTER TABLE rental_index ADD COLUMN IF NOT EXISTS furnishing_version TEXT;

CREATE TABLE IF NOT EXISTS rental_observations (
    listing_id TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    observed_on DATE NOT NULL,
    source TEXT NOT NULL CHECK(source='krisha'),
    rent_period TEXT NOT NULL CHECK(rent_period IN ('monthly','daily')),
    period_source_checked BOOLEAN NOT NULL CHECK(period_source_checked),
    episode_started_at TIMESTAMPTZ NOT NULL,
    city TEXT NOT NULL,
    district TEXT,
    rooms INTEGER NOT NULL CHECK(rooms BETWEEN 1 AND 6),
    price INTEGER NOT NULL CHECK(price>0),
    area REAL,
    complex_name TEXT,
    complex_url TEXT,
    complex_id INTEGER REFERENCES complexes(id),
    hex_id TEXT,
    hex_edge_m REAL,
    furnishing TEXT NOT NULL CHECK(furnishing IN ('none','partial','full','unknown')),
    furnishing_version TEXT,
    PRIMARY KEY(listing_id,observed_on,source,rent_period),
    CHECK(observed_on=(observed_at AT TIME ZONE 'UTC')::date),
    CHECK(episode_started_at<=observed_at),
    CHECK((hex_id IS NULL AND hex_edge_m IS NULL) OR
          (hex_id IS NOT NULL AND hex_edge_m IS NOT NULL AND hex_id ~ '^-?[0-9]+:-?[0-9]+$' AND hex_edge_m BETWEEN 10 AND 1000))
);
CREATE INDEX IF NOT EXISTS idx_rental_observations_period_time ON rental_observations(rent_period,observed_at);

CREATE TABLE IF NOT EXISTS rental_hex_index (
    city TEXT NOT NULL,
    hex_id TEXT NOT NULL CHECK(hex_id ~ '^-?[0-9]+:-?[0-9]+$'),
    hex_edge_m REAL NOT NULL CHECK(hex_edge_m BETWEEN 10 AND 1000),
    rooms INTEGER NOT NULL CHECK(rooms BETWEEN 1 AND 6),
    rent_period TEXT NOT NULL CHECK(rent_period IN ('monthly','daily')),
    furnishing TEXT CHECK(furnishing IN ('none','partial','full')),
    furnishing_version TEXT,
    median_price INTEGER NOT NULL,
    p25_price INTEGER NOT NULL,
    p75_price INTEGER NOT NULL,
    sample_count INTEGER NOT NULL CHECK(sample_count>=5),
    active_supply INTEGER NOT NULL CHECK(active_supply>=0),
    window_days INTEGER NOT NULL CHECK(window_days IN (30,90)),
    source_oldest_at TIMESTAMPTZ NOT NULL,
    source_newest_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    policy_version TEXT NOT NULL,
    CHECK(0<p25_price AND p25_price<=median_price AND median_price<=p75_price),
    CHECK(source_oldest_at<=source_newest_at AND source_newest_at<=updated_at),
    UNIQUE NULLS NOT DISTINCT(city,hex_edge_m,hex_id,rooms,rent_period,furnishing)
);
INSERT INTO app_settings(key,value) VALUES
    ('RENTAL_FURNISHING_INDEX_ENABLED','0'),
    ('RENTAL_FURNISHING_VALIDATION_RECEIPT',''),
    ('RENTAL_FURNISHING_RULES_JSON','{}'),
    ('RENT_MIN_SAMPLE_HEX','5')
ON CONFLICT(key) DO NOTHING;
