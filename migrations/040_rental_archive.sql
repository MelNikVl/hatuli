-- Архивация объявлений аренды (см. задачу "тепловая карта аренды/продажи
-- по последнему месяцу"). До сих пор rental_listings не имела аналога
-- is_active/archived_at, которые есть у apartment_listings — пропавшее с
-- Крыши объявление аренды просто переставало обновлять last_seen, без
-- явной пометки "снято". Зеркалим схему продажи, чтобы:
--   1) можно было проверять актуальность объявлений аренды (см.
--      bot/core/archive_check.py: check_archived_rentals);
--   2) тепловая карта аренды могла показывать "последняя цена перед уходом
--      в архив" за последний месяц вместо пустых гексов.
ALTER TABLE rental_listings ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE rental_listings ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ;
ALTER TABLE rental_listings ADD COLUMN IF NOT EXISTS archive_checked_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_rental_is_active ON rental_listings(is_active);
CREATE INDEX IF NOT EXISTS idx_rental_archived_at ON rental_listings(archived_at);
