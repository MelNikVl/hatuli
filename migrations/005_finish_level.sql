-- 005: отделка объявления (влияет на скор)
ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS finish_level TEXT;
