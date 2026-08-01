-- Когда последний раз обновлялось реальное число просмотров (service_viewcount.py,
-- Playwright) — нужно, чтобы не долбить одно и то же объявление каждый цикл.
ALTER TABLE apartment_listings ADD COLUMN IF NOT EXISTS views_count_updated_at TIMESTAMPTZ;
