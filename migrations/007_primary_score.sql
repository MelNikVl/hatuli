-- 007: отдельная модель скоринга первички + логи для мониторинга разметки

ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS primary_score_total   INTEGER;
ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS primary_score_details JSONB;
