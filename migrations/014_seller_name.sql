-- 014: имя продавца/риелтора с детальной страницы (если Крыша его показывает)
ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS seller_name TEXT;
