-- Задача 2026-08-17 ("Property Identity — photo evidence + review",
-- follow-up после production-развёртывания): scripts/audit_orphan_
-- properties.py (см. коммит 181636e) нашёл 17 osiротевших properties —
-- 15/17 согласуются с тем, что bot/core/archive_check.py делал
-- `DELETE FROM apartment_listings` при подтверждённом 404/410
-- ("страница удалена"), каскадом убирая property_listings (FK ON DELETE
-- CASCADE), но НЕ properties (родитель) — properties, price timeline,
-- true DOM и любая другая история, завязанная на listing_id, теряется
-- НАВСЕГДА в момент DELETE, без возможности восстановить.
--
-- Фикс поведения (bot/core/archive_check.py) — эта миграция только
-- добавляет колонку, ЧТОБЫ различать ДВЕ причины архивации, которые
-- раньше были неразличимы (одна оставляла строку, другая её удаляла):
--   'confirmed_gone'  — страница вернула 404/410, "объявления больше нет"
--   'archived_badge'  — страница жива, но несёт бейдж "В архиве"/
--                        "может быть неактуальным"
-- nullable — существующие archived_at IS NOT NULL строки (архивированы
-- ДО этой миграции) не имеют этого различия и не бэкфилятся задним
-- числом (гадать, какая из двух причин была на тот момент, не будем).
ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS archive_reason TEXT;
ALTER TABLE apartment_listings DROP CONSTRAINT IF EXISTS apartment_listings_archive_reason_check;
ALTER TABLE apartment_listings ADD CONSTRAINT apartment_listings_archive_reason_check
    CHECK (archive_reason IS NULL OR archive_reason = ANY (ARRAY['confirmed_gone', 'archived_badge']));

-- rental_listings — тот же класс бага (check_archived_rentals делает
-- тот же DELETE), тот же принцип "сохранить всю историю", та же колонка.
ALTER TABLE rental_listings ADD COLUMN IF NOT EXISTS archive_reason TEXT;
ALTER TABLE rental_listings DROP CONSTRAINT IF EXISTS rental_listings_archive_reason_check;
ALTER TABLE rental_listings ADD CONSTRAINT rental_listings_archive_reason_check
    CHECK (archive_reason IS NULL OR archive_reason = ANY (ARRAY['confirmed_gone', 'archived_badge']));
