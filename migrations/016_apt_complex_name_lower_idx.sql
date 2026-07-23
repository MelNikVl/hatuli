-- Ускоряет расчёт центроида ЖК для карты аренды (/admin/api/map-points?type=rental):
-- GROUP BY lower(trim(complex_name)) по apartment_listings теперь выполняется
-- один раз за запрос вместо коррелированного подзапроса на каждую строку аренды.
CREATE INDEX IF NOT EXISTS idx_apt_complex_name_lower
    ON apartment_listings (lower(trim(complex_name)))
    WHERE lat IS NOT NULL;
