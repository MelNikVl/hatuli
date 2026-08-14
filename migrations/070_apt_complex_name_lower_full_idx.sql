-- Фаза A.5, п.4 вердикт-стратегии (задача 2026-08-14) — релист-эвристика
-- в outcome_labels_recompute.py join'ит apartment_listings САМА НА СЕБЯ
-- по lower(trim(complex_name))+rooms. Существующие индексы на это
-- выражение (idx_apt_complex_name_lower/idx_apt_lower_trim_complex_name)
-- — ЧАСТИЧНЫЕ (WHERE lat IS NOT NULL), не покрывают архивные объявления
-- без координат; полный (без WHERE) — тот же паттерн, что уже
-- использовался для /admin/api/map-points?type=rental (см. комментарий
-- в bot/core/deal_score.py apply_deal_scores() про migrations/016).
CREATE INDEX IF NOT EXISTS idx_apt_complex_name_lower_full
    ON apartment_listings (lower(trim(complex_name)));
