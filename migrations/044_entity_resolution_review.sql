-- Entity resolution, фаза 1 — доработка по итогам ревью (2026-08-13):
-- см. docs/entity_resolution_plan.md. Раньше record_source_link() писал
-- ЛЮБУЮ связь с confidence >= 0.5 прямо в complex_source_links (спайн),
-- не различая auto-match и review-queue по факту хранения — "очередь на
-- проверку" была только текстом в комментарии, не реальным состоянием.
-- При конфликте (source_id уже привязан к ДРУГОМУ complex_id) — тихо
-- перезаписывал. Ничего не помнило про руками отклонённые пары.

CREATE EXTENSION IF NOT EXISTS pg_trgm;
-- Триграммный индекс — для fuzzy-ступени матчинга (similarity()).
CREATE INDEX IF NOT EXISTS idx_complexes_name_trgm ON complexes USING gin (name gin_trgm_ops);

-- Кандидаты НЕ в спайне: review (confidence 0.5-0.8, ждёт подтверждения)
-- ИЛИ conflict (source_id уже привязан к другому complex_id — новый
-- вариант не применяется молча, ждёт решения).
CREATE TABLE IF NOT EXISTS complex_source_link_candidates (
    id                      SERIAL PRIMARY KEY,
    complex_id              INT NOT NULL REFERENCES complexes(id) ON DELETE CASCADE,
    source                  TEXT NOT NULL,
    source_id               TEXT NOT NULL,
    url                     TEXT,
    match_method            TEXT NOT NULL,
    confidence              NUMERIC NOT NULL,
    kind                    TEXT NOT NULL DEFAULT 'review',  -- 'review' | 'conflict'
    conflict_with_complex_id INT REFERENCES complexes(id),   -- для kind='conflict'
    created_at              TIMESTAMPTZ DEFAULT now(),
    UNIQUE (source, source_id, complex_id)
);
CREATE INDEX IF NOT EXISTS idx_cslc_kind ON complex_source_link_candidates (kind);

-- Память отклонений — конкретную пару (source, source_id, complex_id)
-- больше НЕ предлагаем повторно (record_source_link проверяет перед
-- записью). Ничего не удаляем задним числом — заявка на пересмотр
-- вручную, если понадобится, отдельной задачей.
CREATE TABLE IF NOT EXISTS complex_source_link_rejections (
    id           SERIAL PRIMARY KEY,
    source       TEXT NOT NULL,
    source_id    TEXT NOT NULL,
    complex_id   INT NOT NULL,
    rejected_at  TIMESTAMPTZ DEFAULT now(),
    rejected_by  TEXT,
    UNIQUE (source, source_id, complex_id)
);

-- Без этого приложение (роль krisha, не postgres) падает 500 при первом
-- же обращении — миграции сами по себе создают таблицы с владельцем
-- postgres, права на них не наследуются автоматически (найдено при живой
-- проверке /admin/entity-ids сразу после деплоя).
GRANT SELECT, INSERT, UPDATE, DELETE ON complex_source_link_candidates TO krisha;
GRANT SELECT, INSERT, UPDATE, DELETE ON complex_source_link_rejections TO krisha;
GRANT USAGE, SELECT ON SEQUENCE complex_source_link_candidates_id_seq TO krisha;
GRANT USAGE, SELECT ON SEQUENCE complex_source_link_rejections_id_seq TO krisha;
