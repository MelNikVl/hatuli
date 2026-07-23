-- Бонус к скору за повторные снижения цены (мотивированный продавец):
-- 1-е снижение — без бонуса, 2-е +5, 3-е +10 и т.д. Пересчитывается в
-- service_apartments.py при каждом обнаруженном снижении цены.
ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS price_drop_bonus INT DEFAULT 0;
