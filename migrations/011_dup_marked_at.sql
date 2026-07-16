-- 011: время пометки дублем — для статистики «дублей найдено сегодня»
-- на публичном дашборде. Дедуп сам добавляет колонку при запуске
-- (self-healing), миграция — для явной фиксации схемы.

ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS dup_marked_at TIMESTAMPTZ;
ALTER TABLE rental_listings    ADD COLUMN IF NOT EXISTS dup_marked_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_apt_dup_marked ON apartment_listings (dup_marked_at)
    WHERE is_duplicate = TRUE;
CREATE INDEX IF NOT EXISTS idx_rent_dup_marked ON rental_listings (dup_marked_at)
    WHERE is_duplicate = TRUE;
