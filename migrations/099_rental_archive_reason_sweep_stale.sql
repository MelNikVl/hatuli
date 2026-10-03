-- archive_reason='sweep_stale' и для аренды (задача 2026-10-03, bot/core/stale_archive.py
-- archive_stale_rentals) — тот же класс, что 098 для продажи.
ALTER TABLE rental_listings DROP CONSTRAINT IF EXISTS rental_listings_archive_reason_check;
ALTER TABLE rental_listings ADD CONSTRAINT rental_listings_archive_reason_check
    CHECK (archive_reason IS NULL OR archive_reason = ANY (ARRAY['confirmed_gone', 'archived_badge', 'sweep_stale']));
