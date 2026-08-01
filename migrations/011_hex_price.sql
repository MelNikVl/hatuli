-- 011: гексагональный анализ цены
ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS hex_deal_index NUMERIC;
ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS hex_price_adj INTEGER DEFAULT 0;
ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS hex_details TEXT;
