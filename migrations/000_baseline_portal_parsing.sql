-- Ретроспективный baseline (задача 2026-08-16, "P0 — Integrity") --
-- см. полное объяснение в шапке migrations/000_baseline_users_favorites.sql
-- (первый файл этой группы: почему 000_-префикс, почему без OWNER/GRANT,
-- почему разбито на несколько файлов, механически из pg_dump --schema-only).
--
-- Эта группа: homeportal.kz (Госреестр застройщиков РК) + служебные логи парсеров и каталог ЖК с Krisha.


CREATE TABLE IF NOT EXISTS homeportal_objects (
    object_id integer NOT NULL,
    name text,
    slug text,
    authority text,
    warranty_number text,
    issue_date text,
    start_date text,
    commissioning_date text,
    address text,
    region text,
    latitude text,
    longitude text,
    cadastral_number text,
    developer_bin text,
    developer_name text,
    developer_phone text,
    developer_email text,
    authorized_bin text,
    authorized_name text,
    supervising_bin text,
    supervising_name text,
    tech_bin text,
    tech_name text,
    no_of_houses text,
    no_of_floors text,
    ceiling_height text,
    building_type text,
    wall_filling text,
    facade_finishing text,
    comfort_class text,
    no_of_entrances text,
    passenger_elevators text,
    freight_elevators text,
    parking_places text,
    playgrounds text,
    sports_fields text,
    is_orda_plus text,
    orda_plus_percent text,
    program text,
    program_link text,
    apartments_total integer,
    apartments_sold integer,
    rooms_1 integer,
    rooms_2 integer,
    rooms_3 integer,
    rooms_4 integer,
    apartment_data jsonb,
    images jsonb,
    fetched_at timestamp with time zone,
    matched_complex_id integer,
    matched_at timestamp with time zone,
    match_method text,
    geo_quarantined_at timestamp with time zone,
    geo_quarantine_reason text,
    geo_quarantined_lat text,
    geo_quarantined_lon text,
    PRIMARY KEY (object_id)
);

CREATE TABLE IF NOT EXISTS homeportal_parse_log (
    id SERIAL PRIMARY KEY,
    object_id integer,
    name text,
    status text,
    detail text,
    ts timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS krisha_complex_catalog (
    slug text NOT NULL,
    name text,
    url text,
    found_at timestamp with time zone DEFAULT now(),
    PRIMARY KEY (slug)
);

CREATE TABLE IF NOT EXISTS krisha_parse_log (
    id SERIAL PRIMARY KEY,
    complex_id integer,
    complex_name text,
    apartment_count integer,
    status text,
    detail text,
    ts timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS parser_cycle_history (
    id SERIAL PRIMARY KEY,
    at timestamp with time zone DEFAULT now(),
    duration_sec integer,
    search_requests integer,
    detail_requests integer,
    total_seen integer,
    needs_detail_fetch integer,
    skipped_no_change integer,
    archive_check_requests integer,
    archive_hot_checked integer,
    archive_cold_confirm_checked integer,
    archive_backlog_checked integer
);

CREATE TABLE IF NOT EXISTS parse_errors (
    id SERIAL PRIMARY KEY,
    ts timestamp with time zone DEFAULT now(),
    error_type text,
    message text,
    url text
);

CREATE TABLE IF NOT EXISTS parse_settings (
    key text NOT NULL,
    value text NOT NULL,
    updated_at timestamp with time zone DEFAULT now(),
    PRIMARY KEY (key)
);

CREATE INDEX IF NOT EXISTS idx_homeportal_geo_quarantined ON homeportal_objects (geo_quarantined_at) WHERE (geo_quarantined_at IS NOT NULL);
