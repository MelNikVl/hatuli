-- Привязка объявления к ЖК/дому по id (задача 2026-10-03, план месяца п.3).
-- До этого связь была только текстовой (complex_name ↔ complexes.name) плюс
-- resolved_house_id для домов под зонтиком (0.6% активных). complex_id —
-- единый ключ для аналитики по ЖК; complex_resolution — каким способом
-- получен ('house' — house_resolution, 'name' — однозначное имя, 'geo' —
-- ближайший дом ≤60 м без конкурента в пределах +15 м, только если имени нет).
-- Каждое изменение пишется в listing_complex_resolution_log (миграция 096).
ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS complex_id INTEGER REFERENCES complexes(id);
ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS complex_resolution TEXT;
ALTER TABLE apartment_listings DROP CONSTRAINT IF EXISTS apartment_listings_complex_resolution_check;
ALTER TABLE apartment_listings ADD CONSTRAINT apartment_listings_complex_resolution_check
    CHECK (complex_resolution IS NULL OR complex_resolution = ANY (ARRAY['house', 'name', 'geo']));
CREATE INDEX IF NOT EXISTS idx_apartment_listings_complex_id ON apartment_listings (complex_id);
