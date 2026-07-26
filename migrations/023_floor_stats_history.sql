-- История доли активных объявлений с известным этажом — график на вкладке
-- "Парсеры" (/admin/parser): этаж приходит только с детальной страницы
-- (см. no_photo_stats_history — та же причина, что и с фото).
CREATE TABLE IF NOT EXISTS floor_stats_history (
    id            SERIAL PRIMARY KEY,
    at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    total_active  INT NOT NULL,
    with_floor    INT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_floor_stats_history_at ON floor_stats_history (at);
