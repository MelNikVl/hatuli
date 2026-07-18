-- 012: фото объявлений и ЖК (храним URL с CDN Крыши, не сами файлы)
ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS photos JSONB;
ALTER TABLE complexes ADD COLUMN IF NOT EXISTS photo_url TEXT;
