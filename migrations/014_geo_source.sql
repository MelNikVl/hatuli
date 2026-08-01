-- 014: источник координат объявления
--   krisha  — координаты с детальной страницы Крыши (точные)
--   address — центроид других объявлений с тем же адресом (приблизительные)
ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS geo_source TEXT;
