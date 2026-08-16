-- Ретроспективный baseline (задача 2026-08-16, "P0 — Integrity") --
-- см. полное объяснение в шапке migrations/000_baseline_users_favorites.sql
-- (первый файл этой группы: почему 000_-префикс, почему без OWNER/GRANT,
-- почему разбито на несколько файлов, механически из pg_dump --schema-only).
--
-- Эта группа: прочая статистика/служебное — новости, события, инвест-листинги, история бэкапов и системных метрик.


CREATE TABLE IF NOT EXISTS news (
    id SERIAL PRIMARY KEY,
    ts timestamp with time zone DEFAULT now(),
    title text NOT NULL,
    source text,
    url text,
    image_url text,
    summary text,
    CONSTRAINT news_url_key UNIQUE (url)
);

CREATE TABLE IF NOT EXISTS news_query_stats (
    id BIGSERIAL PRIMARY KEY,
    query text NOT NULL,
    run_at timestamp without time zone DEFAULT now() NOT NULL,
    total integer DEFAULT 0 NOT NULL,
    new_items integer DEFAULT 0 NOT NULL,
    duplicates integer DEFAULT 0 NOT NULL,
    blocked integer DEFAULT 0 NOT NULL,
    errors integer DEFAULT 0 NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id SERIAL PRIMARY KEY,
    type text,
    description text,
    created_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS investment_listings (
    id text NOT NULL,
    url text,
    title text,
    prop_type text,
    price bigint,
    area real,
    address text,
    district text,
    complex_name text,
    city text,
    phone text,
    description text,
    photo_url text,
    published_at text,
    found_at timestamp with time zone DEFAULT now(),
    score_total integer,
    score_yield integer,
    score_location integer,
    score_supply integer,
    score_liquidity integer,
    score_quality integer,
    estimated_yield real,
    estimated_rent integer,
    details_fetched boolean DEFAULT false,
    needs_investigation boolean DEFAULT false,
    is_owner boolean,
    lat real,
    lon real,
    PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS ceiling_stats_history (
    id SERIAL PRIMARY KEY,
    at timestamp with time zone DEFAULT now(),
    total_active integer,
    with_ceiling integer
);

CREATE TABLE IF NOT EXISTS views_coverage_history (
    id SERIAL PRIMARY KEY,
    at timestamp with time zone DEFAULT now(),
    total_active integer,
    with_views integer
);

CREATE TABLE IF NOT EXISTS views_stats_history (
    id SERIAL PRIMARY KEY,
    at timestamp with time zone DEFAULT now(),
    views_1 bigint,
    views_2 bigint,
    views_3 bigint,
    views_4p bigint
);

CREATE TABLE IF NOT EXISTS year_stats_history (
    id SERIAL PRIMARY KEY,
    at timestamp with time zone DEFAULT now() NOT NULL,
    total_active integer NOT NULL,
    with_year integer NOT NULL
);

CREATE TABLE IF NOT EXISTS system_stats_history (
    id SERIAL PRIMARY KEY,
    at timestamp with time zone DEFAULT now(),
    cpu_pct real,
    mem_pct real,
    disk_pct real,
    project_size_gb real
);

CREATE TABLE IF NOT EXISTS backup_history (
    id BIGSERIAL PRIMARY KEY,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    kind text DEFAULT 'manual'::text NOT NULL,
    krisha_mb numeric(8,2),
    hype_mb numeric(8,2),
    project_mb numeric(8,2),
    status text DEFAULT 'ok'::text NOT NULL,
    note text
);

-- backup_history_ts_idx НЕ создаём здесь: та же причина, что
-- idx_listing_fp_listing в 000_baseline_listings_legacy.sql — таблица на
-- проде принадлежит postgres, не krisha, CREATE INDEX падает без
-- владения; индекс уже стоит на проде под этим именем.
