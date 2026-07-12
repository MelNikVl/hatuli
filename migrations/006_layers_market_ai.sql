-- 006: слои скоринга, первичка/вторичка, AI-анализ, обогащение ЖК

-- Слои: суммарная поправка и детализация по каждому слою
ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS layer_bonus   INTEGER DEFAULT 0;
ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS layer_details JSONB;
ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS layers_computed_at TIMESTAMPTZ;

-- Первичка / вторичка
ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS market_type TEXT; -- 'primary' | 'secondary'

-- AI-анализ текста объявления (DeepSeek)
ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS ai_analysis JSONB;
ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS description TEXT;

-- Кеш ответов OSM Overpass (сетка ~110м: координаты, округлённые до 3 знаков)
CREATE TABLE IF NOT EXISTS osm_cache (
    grid_lat   NUMERIC(8,3),
    grid_lon   NUMERIC(8,3),
    kind       TEXT,            -- 'roads' | 'schools'
    payload    JSONB,
    fetched_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (grid_lat, grid_lon, kind)
);

-- Обогащение ЖК с korter/homsters
ALTER TABLE complexes ADD COLUMN IF NOT EXISTS housing_class TEXT;   -- эконом/комфорт/бизнес/премиум
ALTER TABLE complexes ADD COLUMN IF NOT EXISTS korter_url    TEXT;
ALTER TABLE complexes ADD COLUMN IF NOT EXISTS source_info   JSONB;  -- сырые данные с агрегаторов

-- Настройки новых фич (выключены по умолчанию — включаются осознанно)
INSERT INTO app_settings (key, value) VALUES
    ('AI_TEXT_ANALYSIS', '0'),
    ('OSM_LAYERS', '1')
ON CONFLICT (key) DO NOTHING;
