-- Ретроспективный baseline (задача 2026-08-16, "P0 — Integrity") — тот
-- же случай, что уже описан в шапке 000_core_tables.sql, только не 3
-- таблицы, а 55: полная схема живой БД никогда не была в migrations/
-- целиком, только частями (000/078/080 закрывали отдельные находки).
-- Проверено эмпирически: "применить migrations/ с нуля на пустую БД"
-- падало на САМОЙ ПЕРВОЙ миграции (001_alerts.sql — CREATE INDEX ...
-- ON users, а users никогда не создавалась ни одной миграцией).
--
-- Файлы 000_baseline_*.sql (users_favorites/listings_legacy/
-- geo_schools/rentals/portal_parsing/stats_misc) — механически
-- сгенерированы из pg_dump --schema-only живой БД (не руками), только
-- CREATE TABLE IF NOT EXISTS/CREATE INDEX IF NOT EXISTS — идемпотентно
-- как на пустой БД (создаёт), так и на проде (no-op, таблицы уже есть).
-- Без OWNER/GRANT: миграции применяются от имени роли krisha
-- (DATABASE_URL) — krisha и так становится владельцем того, что сама
-- создаёт, в отличие от 078/080 (там таблицы были заведены ролью
-- postgres в обход миграций, GRANT был обязателен).
--
-- Названы с префиксом "000_" (не "082_" и т.п.) НАМЕРЕННО: должны
-- примениться РАНЬШЕ 001_alerts.sql и всех остальных миграций, которые
-- на эти таблицы ссылаются (ALTER/INDEX/FK) — сортировка по имени
-- файла (bot/db/pg.py::_apply_migrations()) гарантирует это для любого
-- "000_XXX.sql" относительно "001_XXX.sql" и позже.
--
-- Внешние ключи НЕ включены сюда (кроме тех, что не создают проблем
-- порядка) — см. migrations/003b_baseline_foreign_keys.sql: один FK
-- (zone_favorites -> priority_zones) ссылается на таблицу, которую
-- создаёт миграция 003, ПОЗЖЕ этого файла — вынесен отдельно, чтобы не
-- усложнять сортировку остальных 54 таблиц.
--
-- Разбивка на отдельные файлы — по темам, ради читаемости диффа (один
-- 1500-строчный файл на 55 таблиц читать невозможно), не по порядку
-- применения (порядок между 000_baseline_*.sql файлами друг относительно
-- друга не важен — между ними самими нет FK-зависимостей).
--
-- Эта группа: users + всё, что ссылается на user_id (избранное,
-- блок-листы, сохранённые поиски, сессии/токены логина, уведомления).
CREATE TABLE IF NOT EXISTS users (
    user_id bigint NOT NULL,
    username text,
    deal_type text,
    city text,
    district text,
    budget_min integer,
    budget_max integer,
    rooms text,
    area_min real,
    move_in text,
    priorities text,
    role integer DEFAULT 1,
    subscription_end timestamp with time zone,
    price_min integer,
    price_max integer,
    area_max real,
    daily_report_hour integer,
    is_blocked integer DEFAULT 0,
    is_paused integer DEFAULT 0,
    location_lat real,
    location_lon real,
    radius_km integer,
    owner_only integer,
    property_type text,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    email text,
    full_name text,
    notify_frequency text DEFAULT 'daily'::text,
    channel_subscribed boolean DEFAULT false,
    last_notified_at timestamp with time zone,
    PRIMARY KEY (user_id)
);

CREATE TABLE IF NOT EXISTS favorites (
    user_id bigint NOT NULL,
    listing_id text NOT NULL,
    saved_at timestamp with time zone DEFAULT now(),
    PRIMARY KEY (user_id, listing_id)
);

CREATE TABLE IF NOT EXISTS favorite_watch_state (
    user_id bigint NOT NULL,
    listing_id text NOT NULL,
    last_description text,
    last_is_active boolean,
    PRIMARY KEY (user_id, listing_id)
);

CREATE TABLE IF NOT EXISTS blocked_listings (
    user_id bigint NOT NULL,
    listing_id text NOT NULL,
    blocked_at timestamp with time zone DEFAULT now(),
    PRIMARY KEY (user_id, listing_id)
);

CREATE TABLE IF NOT EXISTS blocked_photo_urls (
    url text NOT NULL,
    score real,
    reason text,
    created_at timestamp with time zone DEFAULT now(),
    PRIMARY KEY (url)
);

CREATE TABLE IF NOT EXISTS saved_searches (
    id SERIAL PRIMARY KEY,
    user_id bigint,
    listing_id text,
    price_last_seen integer,
    created_at timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS login_tokens (
    token text NOT NULL,
    telegram_id bigint,
    status text DEFAULT 'pending'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    verified_at timestamp with time zone,
    PRIMARY KEY (token)
);

CREATE TABLE IF NOT EXISTS site_sessions (
    session_id text NOT NULL,
    user_id bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    PRIMARY KEY (session_id)
);

CREATE TABLE IF NOT EXISTS user_listings (
    user_id bigint NOT NULL,
    listing_id text NOT NULL,
    notified_at timestamp with time zone,
    PRIMARY KEY (user_id, listing_id)
);

CREATE TABLE IF NOT EXISTS user_listing_notifications (
    user_id bigint NOT NULL,
    listing_id text NOT NULL,
    notified_at timestamp with time zone DEFAULT now(),
    PRIMARY KEY (user_id, listing_id)
);

CREATE TABLE IF NOT EXISTS bot_requests (
    id SERIAL PRIMARY KEY,
    user_id bigint,
    ts timestamp with time zone DEFAULT now()
);

CREATE TABLE IF NOT EXISTS zone_favorites (
    user_id bigint NOT NULL,
    zone_id integer NOT NULL,
    saved_at timestamp with time zone DEFAULT now(),
    PRIMARY KEY (user_id, zone_id)
);

CREATE TABLE IF NOT EXISTS complex_favorites (
    user_id bigint NOT NULL,
    complex_id integer NOT NULL,
    saved_at timestamp with time zone DEFAULT now(),
    PRIMARY KEY (user_id, complex_id)
);
