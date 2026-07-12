-- Настройки приложения: скоринг, монетизация, парсер.
-- Ключ-значение, редактируется из веб-терминала (/admin/settings).

CREATE TABLE IF NOT EXISTS app_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TIMESTAMPTZ DEFAULT now()
);

-- Дефолты (вставляем только если ещё нет)
INSERT INTO app_settings (key, value) VALUES
    ('DEPOSIT_RATE',        '14.0'),  -- ставка депозита KZT, %
    ('APPRECIATION_PCT',    '8.0'),   -- ожидаемый рост цены кв.м, %/год
    ('MORTGAGE_RATE',       '17.0'),  -- рыночная ипотека, %
    ('MORTGAGE_YEARS',      '20'),
    ('MORTGAGE_DOWN_PCT',   '20'),
    ('REALTOR_FEE_PCT',     '2.0'),
    ('ALERT_THRESHOLD',     '65'),    -- минимальный скор для алерта
    ('PARSER_MAX_PAGES',    '5'),     -- страниц krisha за цикл
    ('PARSER_MAX_PRICE',    '80000000'),
    ('MONETIZATION_ENABLED','0')      -- 0 = всё бесплатно, 1 = платный доступ
ON CONFLICT (key) DO NOTHING;

-- На случай если дедупликация ещё не запускалась (top10 фильтрует по этой колонке)
ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS is_duplicate BOOLEAN DEFAULT FALSE;
