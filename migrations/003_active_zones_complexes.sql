-- 003: актуальность объявлений, координаты, зоны приоритета, контакты ЖК

-- Актуальность: архивные объявления помечаются и выпадают из топов
ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS is_active   BOOLEAN DEFAULT TRUE;
ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ;
ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS archive_checked_at TIMESTAMPTZ;

-- Координаты объявления (парсятся со страницы krisha) + бонус за зону
ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS lat REAL;
ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS lon REAL;
ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS zone_bonus INTEGER DEFAULT 0;
ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS zone_name TEXT;

CREATE INDEX IF NOT EXISTS idx_apt_active ON apartment_listings (is_active) WHERE is_active = TRUE;

-- Зоны приоритета: рисуются мышкой на карте в /admin/zones
CREATE TABLE IF NOT EXISTS priority_zones (
    id         SERIAL PRIMARY KEY,
    name       TEXT NOT NULL,
    bonus      INTEGER NOT NULL DEFAULT 10,   -- баллы к скору за попадание в зону
    color      TEXT DEFAULT '#2563eb',
    polygon    JSONB NOT NULL,                -- GeoJSON polygon coordinates [[lon,lat],...]
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Контакты ЖК: ОСИ, управляющая компания, чаты жителей
ALTER TABLE complexes ADD COLUMN IF NOT EXISTS osi_contacts TEXT;
ALTER TABLE complexes ADD COLUMN IF NOT EXISTS uk_name      TEXT;
ALTER TABLE complexes ADD COLUMN IF NOT EXISTS uk_contacts  TEXT;
ALTER TABLE complexes ADD COLUMN IF NOT EXISTS chat_links   TEXT;
ALTER TABLE complexes ADD COLUMN IF NOT EXISTS residents_notes TEXT;
