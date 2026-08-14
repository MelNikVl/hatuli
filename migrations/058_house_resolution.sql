-- House-resolution (задача 2026-08-13, "House-resolution в матчинге
-- apartment_listings") — объявления привязаны к complexes ИМЕНЕМ
-- (lower(trim(complex_name)) = lower(trim(complexes.name))), FK нет и
-- не появляется этой миграцией. Проблема: когда ЖК стал зонтиком, у
-- него могут быть дома (complexes с parent_complex_id), НО объявления
-- продолжают называть ЖК generic-именем зонтика ("ЖК Qaiyndy"), а не
-- конкретного дома ("Qaiyndy 3") — привязать бы их к дому по адресу/
-- токену/гео, но это отдельное, ДОПОЛНИТЕЛЬНОЕ решение поверх
-- name-матчинга, не замена его. resolved_house_id — именно это:
-- "по нашей лучшей попытке, это вот этот конкретный дом under the
-- umbrella", NULL = не уверены, остаётся на зонтике для отображения/
-- аналитики как раньше. См. bot/core/house_resolution.py.
ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS resolved_house_id INT REFERENCES complexes(id) ON DELETE SET NULL;
ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS house_attribution TEXT;        -- 'address' | 'address_geo' | 'token' | 'geo'
ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS house_attribution_detail TEXT; -- человекочитаемое "чем" (для прозрачности на странице дома)
CREATE INDEX IF NOT EXISTS idx_apartment_listings_resolved_house ON apartment_listings (resolved_house_id) WHERE resolved_house_id IS NOT NULL;
