-- История количества активных объявлений без фото — график на /admin/analytics,
-- отвечает на вопрос "почему у большинства объявлений нет фото" (ответ: фото
-- скачиваются только вместе с детальной страницей, а не со страницы поиска —
-- см. COORD_BACKFILL_BATCH в /admin/settings).
CREATE TABLE IF NOT EXISTS no_photo_stats_history (
    id            SERIAL PRIMARY KEY,
    at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    total_active  INT NOT NULL,
    no_photo      INT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_no_photo_stats_history_at ON no_photo_stats_history (at);
