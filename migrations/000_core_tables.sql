-- ============================================================
-- 000: БАЗОВЫЕ ТАБЛИЦЫ, которых не было ни в одной миграции.
--
-- Как так вышло: apartment_listings, complexes и developers были
-- созданы вручную прямо на проде в какой-то момент разработки и
-- никогда не попали в SQL-файлы — на сервере это было не видно
-- (таблицы уже существуют, все ALTER идут через IF NOT EXISTS),
-- но с чистой базы (новый комп, WSL, восстановление после сбоя)
-- проект было НЕВОЗМОЖНО поднять с нуля.
--
-- Файл идемпотентен (CREATE TABLE IF NOT EXISTS) — на проде это
-- будет no-op, на чистой машине — создаст недостающее. Названо 000,
-- чтобы гарантированно применяться до 001-011 (которые ALTER'ят эти
-- таблицы).
-- ============================================================

-- ── Застройщики (алиасы: "Bi Group", "Bi-Group", "BiGroup" → один id) ──
CREATE TABLE IF NOT EXISTS developers (
    id         SERIAL PRIMARY KEY,
    name       TEXT UNIQUE NOT NULL,
    aliases    TEXT[] DEFAULT '{}',
    website    TEXT,
    notes      TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- ── Жилые комплексы ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS complexes (
    id                     SERIAL PRIMARY KEY,
    name                   TEXT NOT NULL,
    district               TEXT,
    developer_id           INTEGER REFERENCES developers(id),
    year_built             INTEGER,
    address                TEXT,

    -- живая статистика (пересчитывается каждый цикл парсера продаж)
    listings_count         INTEGER DEFAULT 0,   -- активных в продаже сейчас
    sold_count             INTEGER DEFAULT 0,   -- ушло в архив за историю
    avg_price_m2           NUMERIC,
    avg_yield              NUMERIC,
    rental_listings_count  INTEGER DEFAULT 0,

    -- инфраструктура / удобства (редактируется вручную в /admin/complexes)
    has_parking            BOOLEAN DEFAULT FALSE,
    has_security            BOOLEAN DEFAULT FALSE,
    has_closed_territory    BOOLEAN DEFAULT FALSE,
    has_playground          BOOLEAN DEFAULT FALSE,
    school_distance_m       INTEGER,
    lrt_distance_m           INTEGER,
    notes                   TEXT,

    -- контакты ЖК
    osi_contacts            TEXT,
    uk_name                 TEXT,
    uk_contacts              TEXT,
    chat_links               TEXT,
    residents_notes           TEXT,

    -- обогащение с агрегаторов (korter.kz, homsters.kz)
    housing_class            TEXT,       -- эконом/комфорт/бизнес/премиум
    korter_url                TEXT,
    source_info                JSONB,     -- сырые данные по источникам: {"korter": {...}, "homsters": {...}}

    created_at                TIMESTAMPTZ DEFAULT now(),
    updated_at                TIMESTAMPTZ DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_complexes_name_lower ON complexes (lower(name));
CREATE INDEX IF NOT EXISTS idx_complexes_district ON complexes (district);

-- ── Объявления о продаже квартир ────────────────────────────────────
CREATE TABLE IF NOT EXISTS apartment_listings (
    id              TEXT PRIMARY KEY,     -- id объявления с Крыши (цифры из URL)
    url             TEXT,
    title           TEXT,
    price           BIGINT,
    area            REAL,
    rooms           INTEGER,
    address         TEXT,
    district        TEXT,
    complex_name    TEXT,

    -- доходность (из rental_index)
    est_rent        BIGINT DEFAULT 0,
    yield_pct       NUMERIC DEFAULT 0,
    payback_years   NUMERIC,
    rent_source     TEXT,

    -- скоринг: база 0-100 + компоненты
    score_total          INTEGER DEFAULT 0,
    score_yield           INTEGER DEFAULT 0,
    score_price_market    INTEGER DEFAULT 0,
    score_location         INTEGER DEFAULT 0,
    score_apt_type          INTEGER DEFAULT 0,
    score_floor              INTEGER DEFAULT 0,
    score_complex             INTEGER DEFAULT 0,
    score_supply               INTEGER DEFAULT 0,
    reasons                     TEXT,          -- JSON-массив причин скора

    -- отделка / зоны / слои локации (надбавки к базовому скору)
    finish_level        TEXT,
    zone_bonus           INTEGER DEFAULT 0,
    zone_name             TEXT,
    layer_bonus            INTEGER DEFAULT 0,
    layer_details            JSONB,
    layers_computed_at        TIMESTAMPTZ,

    -- отдельная модель скоринга для первички
    market_type          TEXT,           -- 'primary' | 'secondary'
    primary_score_total    INTEGER,
    primary_score_details    JSONB,

    -- характеристики объекта
    description        TEXT,
    floor               INTEGER,
    floors_total          INTEGER,
    year_built              INTEGER,
    building_type             TEXT,
    renovation                  TEXT,
    furniture                     TEXT,
    is_new_build                   BOOLEAN DEFAULT FALSE,
    developer_name                   TEXT,
    seller_type                        TEXT,
    is_owner                             BOOLEAN,
    -- Скоринг доверия (задача 2026-08-13) — пока один параметр (seller_type):
    -- 1.0 Крыша Агент (label-user-agent на карточке, тот же сигнал, что
    -- фильтр "От Крыша Агентов"), 0.8 собственник (is_owner), 0.6 обычный
    -- риелтор без бейджа. См. bot/core/apartment_parser.py.
    trust_score                        NUMERIC,

    -- торг / аналоги
    bargain_target        BIGINT,
    bargain_discount_pct    NUMERIC,
    bargain_rec               TEXT,

    -- координаты (только со страницы объявления, не с карточки в выдаче)
    lat    REAL,
    lon    REAL,
    coord_fetch_attempted_at    TIMESTAMPTZ,

    -- AI-анализ текста (DeepSeek)
    ai_analysis    JSONB,

    -- дедупликация
    is_duplicate     BOOLEAN DEFAULT FALSE,
    duplicate_of       TEXT,
    dup_marked_at         TIMESTAMPTZ,

    -- жизненный цикл
    details_fetched    BOOLEAN DEFAULT FALSE,
    is_active            BOOLEAN DEFAULT TRUE,
    archived_at             TIMESTAMPTZ,
    archive_checked_at        TIMESTAMPTZ,
    notified                    BOOLEAN DEFAULT FALSE,

    first_seen    TIMESTAMPTZ DEFAULT now(),
    last_seen       TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_apt_score      ON apartment_listings (score_total DESC);
CREATE INDEX IF NOT EXISTS idx_apt_district   ON apartment_listings (district);
CREATE INDEX IF NOT EXISTS idx_apt_complex    ON apartment_listings (complex_name);
CREATE INDEX IF NOT EXISTS idx_apt_last_seen  ON apartment_listings (last_seen);
CREATE INDEX IF NOT EXISTS idx_apt_first_seen ON apartment_listings (first_seen);
CREATE INDEX IF NOT EXISTS idx_apt_market_type ON apartment_listings (market_type);
