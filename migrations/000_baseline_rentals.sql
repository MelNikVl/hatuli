-- Ретроспективный baseline (задача 2026-08-16, "P0 — Integrity") --
-- см. полное объяснение в шапке migrations/000_baseline_users_favorites.sql
-- (первый файл этой группы: почему 000_-префикс, почему без OWNER/GRANT,
-- почему разбито на несколько файлов, механически из pg_dump --schema-only).
--
-- Эта группа: аренда — rental_index/rental_listings/rental_price_history, отдельный контур от apartment_listings (продажа).


CREATE TABLE IF NOT EXISTS rental_index (
    id SERIAL PRIMARY KEY,
    city text NOT NULL,
    district text,
    complex_name text,
    rooms integer,
    prop_type text DEFAULT 'apartment'::text NOT NULL,
    median_price integer,
    avg_price integer,
    p25_price integer,
    p75_price integer,
    sample_count integer,
    price_per_sqm integer,
    updated_at timestamp with time zone DEFAULT now(),
    area_min real,
    area_max real,
    CONSTRAINT rental_index_city_district_complex_name_rooms_prop_type_key UNIQUE NULLS NOT DISTINCT (city, district, complex_name, rooms, prop_type)
);

CREATE TABLE IF NOT EXISTS rental_listings (
    id text NOT NULL,
    url text,
    title text,
    price integer,
    area real,
    rooms integer,
    floor integer,
    floors_total integer,
    address text,
    district text,
    complex_name text,
    city text,
    prop_type text DEFAULT 'apartment'::text,
    lat real,
    lon real,
    published_at text,
    found_at timestamp with time zone DEFAULT now(),
    is_duplicate boolean DEFAULT false,
    duplicate_of text,
    photo_url text,
    last_seen timestamp with time zone,
    dup_marked_at timestamp with time zone,
    dup_match text,
    complex_url text,
    coord_fetch_attempted_at timestamp with time zone,
    photos jsonb,
    dup_needs_review boolean DEFAULT false,
    is_active boolean DEFAULT true NOT NULL,
    archived_at timestamp with time zone,
    archive_checked_at timestamp with time zone,
    PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS rental_price_history (
    id SERIAL PRIMARY KEY,
    listing_id text NOT NULL,
    old_price integer,
    new_price integer,
    changed_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rent_dup_marked ON rental_listings (dup_marked_at) WHERE (is_duplicate = true);
CREATE INDEX IF NOT EXISTS idx_rental_archived_at ON rental_listings (archived_at);
CREATE INDEX IF NOT EXISTS idx_rental_complex ON rental_listings (complex_name);
CREATE INDEX IF NOT EXISTS idx_rental_district ON rental_listings (district);
CREATE INDEX IF NOT EXISTS idx_rental_found_at ON rental_listings (found_at);
CREATE INDEX IF NOT EXISTS idx_rental_index_complex ON rental_index (complex_name);
CREATE INDEX IF NOT EXISTS idx_rental_index_district ON rental_index (district, rooms);
CREATE INDEX IF NOT EXISTS idx_rental_is_active ON rental_listings (is_active);
CREATE INDEX IF NOT EXISTS idx_rental_last_seen ON rental_listings (last_seen);
CREATE INDEX IF NOT EXISTS idx_rental_ph_listing ON rental_price_history (listing_id, changed_at);
CREATE INDEX IF NOT EXISTS idx_rental_prop_type ON rental_listings (prop_type);
CREATE INDEX IF NOT EXISTS idx_rental_rooms ON rental_listings (rooms);
