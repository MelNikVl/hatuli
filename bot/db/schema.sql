-- ============================================================
--  ai_molt2  PostgreSQL schema  (v2)
--  Run: psql -U krisha -d krisha_bot -f schema.sql
-- ============================================================

CREATE TABLE IF NOT EXISTS users (
    user_id           BIGINT PRIMARY KEY,
    username          TEXT,
    deal_type         TEXT,
    city              TEXT,
    district          TEXT,
    budget_min        INTEGER,
    budget_max        INTEGER,
    rooms             TEXT,
    area_min          REAL,
    move_in           TEXT,
    priorities        TEXT,
    role              INTEGER DEFAULT 1,
    subscription_end  TIMESTAMPTZ,
    price_min         INTEGER,
    price_max         INTEGER,
    area_max          REAL,
    daily_report_hour INTEGER,
    is_blocked        INTEGER DEFAULT 0,
    is_paused         INTEGER DEFAULT 0,
    location_lat      REAL,
    location_lon      REAL,
    radius_km         INTEGER,
    owner_only        INTEGER,
    property_type     TEXT,
    created_at        TIMESTAMPTZ DEFAULT NOW(),
    updated_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS listings (
    id            TEXT PRIMARY KEY,
    url           TEXT,
    title         TEXT,
    price         BIGINT,
    area          REAL,
    rooms         INTEGER,
    floor         INTEGER,
    floors_total  INTEGER,
    address       TEXT,
    district      TEXT,
    city          TEXT,
    deal_type     TEXT,
    phone         TEXT,
    complex_name  TEXT,
    photo_url     TEXT,
    photo_hash    TEXT,
    photo_urls    TEXT,
    published_at  TEXT,
    found_at      TIMESTAMPTZ DEFAULT NOW(),
    sources       TEXT,
    lat           REAL,
    lon           REAL
);

CREATE TABLE IF NOT EXISTS investment_listings (
    id                  TEXT PRIMARY KEY,
    url                 TEXT,
    title               TEXT,
    prop_type           TEXT,
    price               BIGINT,
    area                REAL,
    address             TEXT,
    district            TEXT,
    complex_name        TEXT,
    city                TEXT,
    phone               TEXT,
    description         TEXT,
    photo_url           TEXT,
    published_at        TEXT,
    found_at            TIMESTAMPTZ DEFAULT NOW(),
    score_total         INTEGER,
    score_yield         INTEGER,
    score_location      INTEGER,
    score_supply        INTEGER,
    score_liquidity     INTEGER,
    score_quality       INTEGER,
    estimated_yield     REAL,
    estimated_rent      INTEGER,
    details_fetched     BOOLEAN DEFAULT FALSE,
    needs_investigation BOOLEAN DEFAULT FALSE,
    is_owner            BOOLEAN,
    lat                 REAL,
    lon                 REAL
);

-- ============================================================
--  RENTAL LISTINGS — сырые объявления об аренде
-- ============================================================
CREATE TABLE IF NOT EXISTS rental_listings (
    id            TEXT PRIMARY KEY,
    url           TEXT,
    title         TEXT,
    price         INTEGER,
    area          REAL,
    rooms         INTEGER,
    floor         INTEGER,
    floors_total  INTEGER,
    address       TEXT,
    district      TEXT,
    complex_name  TEXT,
    city          TEXT,
    prop_type     TEXT DEFAULT 'apartment',
    lat           REAL,
    lon           REAL,
    published_at  TEXT,
    found_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_rental_district  ON rental_listings(district);
CREATE INDEX IF NOT EXISTS idx_rental_complex   ON rental_listings(complex_name);
CREATE INDEX IF NOT EXISTS idx_rental_rooms     ON rental_listings(rooms);
CREATE INDEX IF NOT EXISTS idx_rental_prop_type ON rental_listings(prop_type);
CREATE INDEX IF NOT EXISTS idx_rental_found_at  ON rental_listings(found_at);

-- ============================================================
--  RENTAL INDEX — агрегированные ставки аренды
--  Пересчитывается каждые 6 часов
-- ============================================================
CREATE TABLE IF NOT EXISTS rental_index (
    id            SERIAL PRIMARY KEY,
    city          TEXT NOT NULL,
    district      TEXT,
    complex_name  TEXT,
    rooms         INTEGER,
    prop_type     TEXT NOT NULL DEFAULT 'apartment',
    median_price  INTEGER,
    avg_price     INTEGER,
    p25_price     INTEGER,
    p75_price     INTEGER,
    sample_count  INTEGER,
    price_per_sqm INTEGER,
    updated_at    TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (city, district, complex_name, rooms, prop_type)
);

CREATE INDEX IF NOT EXISTS idx_rental_index_complex  ON rental_index(complex_name);
CREATE INDEX IF NOT EXISTS idx_rental_index_district ON rental_index(district, rooms);

CREATE TABLE IF NOT EXISTS favorites (
    user_id       BIGINT,
    listing_id    TEXT,
    saved_at      TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (user_id, listing_id)
);

CREATE TABLE IF NOT EXISTS blocked_listings (
    user_id       BIGINT,
    listing_id    TEXT,
    blocked_at    TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (user_id, listing_id)
);

CREATE TABLE IF NOT EXISTS saved_searches (
    id               SERIAL PRIMARY KEY,
    user_id          BIGINT,
    listing_id       TEXT,
    price_last_seen  INTEGER,
    created_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS user_listing_notifications (
    user_id       BIGINT,
    listing_id    TEXT,
    notified_at   TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (user_id, listing_id)
);

CREATE TABLE IF NOT EXISTS listing_views (
    id            SERIAL PRIMARY KEY,
    user_id       BIGINT,
    listing_id    TEXT,
    action        TEXT,
    ts            TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS ai_cache (
    listing_id    TEXT,
    user_id       BIGINT,
    explanation   TEXT,
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (listing_id, user_id)
);

CREATE TABLE IF NOT EXISTS user_listings (
    user_id       BIGINT,
    listing_id    TEXT,
    notified_at   TIMESTAMPTZ,
    PRIMARY KEY (user_id, listing_id)
);

CREATE TABLE IF NOT EXISTS events (
    id            SERIAL PRIMARY KEY,
    type          TEXT,
    description   TEXT,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS bot_requests (
    id            SERIAL PRIMARY KEY,
    user_id       BIGINT,
    ts            TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS parse_errors (
    id            SERIAL PRIMARY KEY,
    ts            TIMESTAMPTZ DEFAULT NOW(),
    error_type    TEXT,
    message       TEXT,
    url           TEXT
);
