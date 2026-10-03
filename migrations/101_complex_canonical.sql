-- Канонический ЖК для аналитики (задача 2026-10-03, «усиление точности», п.2).
-- Не сливаем записи complexes физически (много FK, и сливать дубли без
-- человека — против решения в 095_complex_relations), а помечаем:
--   canonical_id     — какой записью считать этот ЖК в аналитике (NULL = сам собой);
--   canonical_reason — 'krisha_slug' (Крыша сама считает это одним ЖК:
--                      одинаковая ссылка /complex/show/<город>/<slug>/),
--                      'name_prefix' (имя — обрывок текста объявления, начинается
--                      с имени реального ЖК), 'junk_unmatched' (обрывок текста без
--                      реального ЖК — в аналитике не используется).
-- Пересчитывается bot/core/complex_canonical.py; apartment_listings.complex_id
-- всегда указывает на канонический ЖК (bot/core/complex_binding.py).
ALTER TABLE complexes ADD COLUMN IF NOT EXISTS canonical_id INTEGER REFERENCES complexes(id);
ALTER TABLE complexes ADD COLUMN IF NOT EXISTS canonical_reason TEXT;
ALTER TABLE complexes DROP CONSTRAINT IF EXISTS complexes_canonical_reason_check;
ALTER TABLE complexes ADD CONSTRAINT complexes_canonical_reason_check
    CHECK (canonical_reason IS NULL OR canonical_reason = ANY (ARRAY['krisha_slug', 'name_prefix', 'junk_unmatched']));
ALTER TABLE apartment_listings DROP CONSTRAINT IF EXISTS apartment_listings_complex_resolution_check;
ALTER TABLE apartment_listings ADD CONSTRAINT apartment_listings_complex_resolution_check
    CHECK (complex_resolution IS NULL OR complex_resolution = ANY (ARRAY['house', 'url', 'name', 'geo']));
