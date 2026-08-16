-- Ретроспективный baseline (задача 2026-08-16, "P0 — Integrity") --
-- см. полное объяснение в шапке migrations/000_baseline_users_favorites.sql
-- (первый файл этой группы: почему 000_-префикс, почему без OWNER/GRANT,
-- почему разбито на несколько файлов, механически из pg_dump --schema-only).
--
-- Эта группа: гео-справочники — школы/садики (astana_schools/kindergartens), транспортные гексагоны и остановки, снос, дороги, годы застройки по адресу.
--
-- Читаются bot/core/location_score.py напрямую (astana_schools/
-- kindergartens, transport_hexes, demolition_houses) — не архив,
-- живой путь скоринга.

CREATE TABLE IF NOT EXISTS astana_schools (
    id SERIAL PRIMARY KEY,
    name text,
    address text,
    lat double precision,
    lon double precision,
    type text,
    language text,
    capacity integer,
    actual_students integer,
    rating real,
    year_opened integer,
    district text,
    phone text,
    website text,
    source text,
    rating_2gis real,
    reviews_count_2gis integer,
    geo_2gis_id text,
    rating_fetched_at timestamp with time zone
);

CREATE TABLE IF NOT EXISTS astana_kindergartens (
    id SERIAL PRIMARY KEY,
    name text,
    address text,
    lat double precision,
    lon double precision,
    type text,
    age_groups text,
    capacity integer,
    rating real,
    district text,
    phone text,
    website text,
    source text,
    rating_2gis real,
    reviews_count_2gis integer,
    geo_2gis_id text,
    rating_fetched_at timestamp with time zone
);

CREATE TABLE IF NOT EXISTS transport_hexes (
    id SERIAL PRIMARY KEY,
    lat double precision,
    lon double precision,
    score real,
    dist_lrt real,
    dist_bus real,
    dist_road real,
    dist_junction real,
    route_count integer
);

CREATE TABLE IF NOT EXISTS transport_stops (
    id SERIAL PRIMARY KEY,
    name text,
    lat double precision,
    lon double precision
);

CREATE TABLE IF NOT EXISTS demolition_houses (
    id SERIAL PRIMARY KEY,
    address text NOT NULL,
    district text,
    apartments integer,
    demolish_year integer,
    year_built integer,
    wear_pct numeric(5,1),
    lat double precision,
    lon double precision,
    geocoded_at timestamp with time zone,
    source text DEFAULT 'gov.kz перечень 18.06.2026'::text,
    created_at timestamp with time zone DEFAULT now(),
    CONSTRAINT demolition_houses_address_demolish_year_key UNIQUE (address, demolish_year)
);

CREATE TABLE IF NOT EXISTS city_roads (
    id SERIAL PRIMARY KEY,
    lat double precision NOT NULL,
    lon double precision NOT NULL,
    lanes integer NOT NULL,
    highway text NOT NULL,
    updated_at timestamp with time zone DEFAULT now(),
    CONSTRAINT city_roads_lat_lon_highway_key UNIQUE (lat, lon, highway)
);

CREATE TABLE IF NOT EXISTS house_years (
    address text NOT NULL,
    year_built integer,
    listings_cnt integer,
    source text DEFAULT 'krisha_listings'::text,
    is_old_fund boolean DEFAULT false NOT NULL,
    PRIMARY KEY (address)
);
