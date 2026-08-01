-- 009: отслеживание попыток докачки координат (чтобы не долбить бесконечно
-- одно и то же объявление, но и не бросать его навсегда)
ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS coord_fetch_attempted_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_apt_coord_backfill
    ON apartment_listings (coord_fetch_attempted_at)
    WHERE lat IS NULL AND is_active IS NOT FALSE;
