-- Зонтик/дом (umbrella/house) — задача 2026-08-13, см.
-- docs/entity_resolution_plan.md ("расшивка — модель зонтик/дом").
-- Отдельно от provenance.split_from (JSONB, история происхождения,
-- неизменяемая) — parent_complex_id структурная, ВСЕГДА-текущая
-- принадлежность "дом -> зонтик", обычная FK-колонка (не JSONB) —
-- нужна для JOIN на публичных страницах (список домов зонтика,
-- ссылка "часть комплекса X" на странице дома) и в админке
-- (сортировка/фильтр), а не только для чтения истории.
ALTER TABLE complexes ADD COLUMN IF NOT EXISTS parent_complex_id INT REFERENCES complexes(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_complexes_parent ON complexes (parent_complex_id) WHERE parent_complex_id IS NOT NULL;
