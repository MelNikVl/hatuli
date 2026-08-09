-- Денормализуем developer_id прямо на newbuild_units — нужен для:
--  1) графика "спарсено во времени" с фильтром по застройщику на
--     /admin/parsers?tab=novostroyki (без JOIN на complexes в hot path);
--  2) панели выбора застройщиков на карте (фильтр набора точек).
ALTER TABLE newbuild_units ADD COLUMN IF NOT EXISTS developer_id INTEGER REFERENCES developers(id);

UPDATE newbuild_units u SET developer_id = c.developer_id
FROM complexes c WHERE c.id = u.complex_id AND u.developer_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_newbuild_units_developer ON newbuild_units(developer_id);
