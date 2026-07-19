-- 013: собственные координаты ЖК (центроид объявлений или с страниц korter/homsters)
ALTER TABLE complexes ADD COLUMN IF NOT EXISTS lat DOUBLE PRECISION;
ALTER TABLE complexes ADD COLUMN IF NOT EXISTS lon DOUBLE PRECISION;
ALTER TABLE complexes ADD COLUMN IF NOT EXISTS coords_source TEXT; -- listings|korter|homsters
