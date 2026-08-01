-- 004: экспозиция аренды + прочее

-- Аренда: отделяем "первый раз увидели" от "последний раз видели",
-- чтобы считать время экспозиции (как быстро уходят квартиры в аренду)
ALTER TABLE rental_listings ADD COLUMN IF NOT EXISTS last_seen TIMESTAMPTZ;
UPDATE rental_listings SET last_seen = found_at WHERE last_seen IS NULL;

CREATE INDEX IF NOT EXISTS idx_rental_last_seen ON rental_listings(last_seen);
