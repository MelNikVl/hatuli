-- Ретроспективный baseline (задача 2026-08-16, "P0 — Integrity") --
-- см. полное объяснение в шапке migrations/000_baseline_users_favorites.sql
-- (первый файл этой группы: почему 000_-префикс, почему без OWNER/GRANT,
-- почему разбито на несколько файлов, механически из pg_dump --schema-only).
--
-- Эта группа: листинги/legacy per-listing-таблицы: просмотры, планировки, AI-кэш, скоры ЖК, отзывы/техспеки/материалы/стены/окна/двери/бетон ЖК, программы застройщика и банков.
--
-- listings (не apartment_listings!) — более ранняя/параллельная
-- таблица объявлений, используется в 8 тестовых файлах и десятках мест
-- в bot/ — не мёртвый код, отдельный от apartment_listings контур.

CREATE TABLE IF NOT EXISTS listings (
    id text NOT NULL,
    url text,
    title text,
    price bigint,
    area real,
    rooms integer,
    floor integer,
    floors_total integer,
    address text,
    district text,
    city text,
    deal_type text,
    phone text,
    complex_name text,
    photo_url text,
    photo_hash text,
    photo_urls text,
    published_at text,
    found_at timestamp with time zone DEFAULT now(),
    sources text,
    lat real,
    lon real,
    PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS listing_views (
    id SERIAL PRIMARY KEY,
    user_id bigint,
    listing_id text,
    action text,
    ts timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS listing_floorplans (
    id SERIAL PRIMARY KEY,
    listing_id text NOT NULL,
    photo_url text,
    floorplan_score real,
    other_score real,
    is_floorplan boolean,
    checked_at timestamp with time zone DEFAULT now(),
    h_sat real,
    h_white real,
    h_gray real,
    h_ortho real,
    h_ink real
);

CREATE TABLE IF NOT EXISTS ai_cache (
    listing_id text NOT NULL,
    user_id bigint NOT NULL,
    explanation text,
    created_at timestamp with time zone DEFAULT now(),
    PRIMARY KEY (listing_id, user_id)
);

CREATE TABLE IF NOT EXISTS complex_scores (
    complex_name character varying(200) NOT NULL,
    rooms integer NOT NULL,
    avg_score numeric,
    median_price numeric,
    yield_pct numeric,
    listings_count integer,
    trend_30d numeric,
    recommendation text,
    updated_at timestamp without time zone DEFAULT now(),
    PRIMARY KEY (complex_name, rooms)
);

CREATE TABLE IF NOT EXISTS complex_reviews (
    id SERIAL PRIMARY KEY,
    complex_id integer NOT NULL,
    user_id bigint NOT NULL,
    rating integer,
    text text NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    CONSTRAINT complex_reviews_complex_id_user_id_key UNIQUE (complex_id, user_id)
);

CREATE TABLE IF NOT EXISTS complex_tech_specs (
    complex_id integer NOT NULL,
    construction_type text,
    concrete_class text,
    rebar_class text,
    facade_type text,
    insulation_material text,
    insulation_thickness_mm integer,
    heating_type text,
    heating_details text,
    ventilation_type text,
    lifts_brand text,
    lifts_model text,
    lifts_count_per_section integer,
    ceiling_height_min numeric,
    ceiling_height_max numeric,
    developer_bin text,
    elicense_status text,
    elicense_checked_at timestamp with time zone,
    docs_psd_expertise_number text,
    docs_psd_expertise_date date,
    docs_apz_number text,
    docs_apz_date date,
    docs_commission_act_number text,
    docs_commission_act_date date,
    notes text,
    updated_at timestamp with time zone DEFAULT now(),
    floors_total integer,
    lifts_type text,
    window_type text,
    window_height_min numeric,
    window_height_max numeric,
    PRIMARY KEY (complex_id)
);

CREATE TABLE IF NOT EXISTS complex_materials (
    id SERIAL PRIMARY KEY,
    complex_id integer NOT NULL,
    facade text,
    walls text,
    windows text,
    elevators text,
    heating text,
    doors text,
    notes text,
    source_name text,
    source_url text,
    created_at timestamp with time zone DEFAULT now(),
    CONSTRAINT complex_materials_complex_id_source_name_key UNIQUE (complex_id, source_name)
);

CREATE TABLE IF NOT EXISTS complex_walls (
    id SERIAL PRIMARY KEY,
    complex_id integer NOT NULL,
    wall_type text,
    layer_order integer,
    material text,
    thickness_mm integer,
    thermal_resistance_r numeric
);

CREATE TABLE IF NOT EXISTS complex_windows (
    id SERIAL PRIMARY KEY,
    complex_id integer NOT NULL,
    frame_material text,
    profile_brand text,
    profile_model text,
    profile_chambers_count integer,
    glass_unit_chambers_count integer,
    glass_types text,
    uw_coefficient numeric
);

CREATE TABLE IF NOT EXISTS complex_doors (
    id SERIAL PRIMARY KEY,
    complex_id integer NOT NULL,
    door_type text,
    leaf_material text,
    steel_thickness_mm numeric,
    filler_type text,
    sound_insulation_db numeric
);

CREATE TABLE IF NOT EXISTS complex_concrete_rebar (
    id SERIAL PRIMARY KEY,
    complex_id integer NOT NULL,
    element_type text,
    concrete_class text,
    rebar_class text,
    rebar_scheme_description text
);

CREATE TABLE IF NOT EXISTS developer_programs (
    id SERIAL PRIMARY KEY,
    developer_id integer NOT NULL,
    title text NOT NULL,
    description text,
    url text,
    source text,
    sort_order integer DEFAULT 0,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    CONSTRAINT developer_programs_developer_id_title_key UNIQUE (developer_id, title)
);

CREATE TABLE IF NOT EXISTS mortgage_programs (
    id SERIAL PRIMARY KEY,
    bank_id integer,
    name text NOT NULL,
    housing_type text DEFAULT 'both'::text NOT NULL,
    rate_min real,
    rate_max real,
    rate_note text,
    down_payment_min_pct integer,
    max_amount_tg bigint,
    max_term_years integer,
    conditions text,
    source_url text,
    updated_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS banks (
    id SERIAL PRIMARY KEY,
    slug text NOT NULL,
    name text NOT NULL,
    short_name text,
    description text,
    website text,
    phone text,
    program_type text DEFAULT 'bank'::text,
    notes text,
    sort_order integer DEFAULT 100,
    updated_at timestamp with time zone DEFAULT now(),
    appraisal_notes text,
    logo_url text,
    CONSTRAINT banks_slug_key UNIQUE (slug)
);

-- idx_listing_fp_listing (listing_floorplans) НЕ создаём здесь: таблица
-- на проде принадлежит роли postgres (не krisha), CREATE INDEX без
-- владения падает InsufficientPrivilegeError — проверено эмпирически
-- на копии прода (CREATE DATABASE ... TEMPLATE krisha_bot). Индекс уже
-- стоит на проде под этим именем (создан вместе с самой таблицей, в
-- обход миграций) — эта миграция и так no-op там. На пустой БД (CI)
-- индекс не появится — это единственный реальный пробел от такого
-- решения, не критичный: тесты не проверяют наличие ИМЕННО этого
-- индекса, только данные.
