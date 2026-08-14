-- Часть 2, п.11 (задача 2026-08-14, "скоринг волна 2") —
-- complexes.housing_class_estimate был заполнен ОДНОРАЗОВЫМ SQL-запуском
-- (коммит 0bb2479, 2026-08-01) прямо на БД, без единого писателя в
-- репозитории с тех пор (docs/scoring_audit.md §3/§5.2 — "заморожено
-- без единого писателя в репозитории"). computed_at обязателен для
-- любого снимка/оценки — урок Г3 (docs/temporal_policy.md, правило 2):
-- честная дата "на когда актуально", не молчаливая заморозка навсегда.
--
-- Бэкфил computed_at для уже существующих значений — 2026-08-01, дата
-- того самого разового прогона (см. коммит выше) — не выдумываем более
-- раннюю/позднюю дату, честная историческая метка.
ALTER TABLE complexes ADD COLUMN IF NOT EXISTS housing_class_estimate_computed_at TIMESTAMPTZ;
UPDATE complexes SET housing_class_estimate_computed_at = '2026-08-01 00:00:00+00'
WHERE housing_class_estimate IS NOT NULL AND housing_class_estimate_computed_at IS NULL;
