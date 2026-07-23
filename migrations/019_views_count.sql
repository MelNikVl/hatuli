-- Число просмотров объявления на Крыше (забирается отдельным POST-запросом
-- к внутреннему сервису Крыши — в статичном HTML его нет).
ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS views_count INT;
