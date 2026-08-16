-- Фаза «Отзывы из нескольких источников» (задача 2026-08-16) —
-- reviews_raw: СЫРЫЕ отзывы на ЖК/застройщика из всех источников
-- (2gis / google_maps / yandex), собранные reviews_pipeline.py
-- (ежедневно, krisha-reviews-collect.timer, 03:00).
--
-- Зачем отдельная таблица рядом с developer_reviews (миграция 078):
-- developer_reviews — КЛАССИФИЦИРОВАННЫЕ отзывы 2GIS (sentiment/topics
-- заполнены на входе LLM'ом при сборе). reviews_raw — слой сырых данных
-- мульти-источникового конвейера: сначала собрать и дедуплицировать,
-- классификация (sentiment/topics, задача DeepSeek) — отдельным проходом
-- ПОЗЖЕ по строкам с classified_at IS NULL. Сырые данные не переписываем
-- под результат классификации — можно переклассифицировать задним числом
-- без пересбора (тот же принцип «хранить, а не считать live», что в
-- complex_location_scores, миграция 072).
--
-- text_hash — sha1 нормализованного текста (lower, схлопнутые пробелы),
-- считает писатель. Дедупликация ДВУХ уровней:
--   1) внутри батча оркестратор схлопывает кросс-посты одного отзыва в
--      разные источники по (author, review_date, text_hash) — остаётся
--      один (первый по приоритету источника), источники-дубли видны в
--      raw->>'also_on';
--   2) UNIQUE (complex_id, source, text_hash) — идемпотентность
--      повторных прогонов одного источника (тот же паттерн, что
--      UNIQUE(complex_id, source_entity_id, review_text) в
--      developer_reviews). complex_id nullable (матчинг к ЖК не всегда
--      резолвится — отзыв «на застройщика» без ЖК тоже валиден), поэтому
--      в UNIQUE он вместе с COALESCE-ключом не нужен: NULL complex_id
--      допускает дубли — осознанно, такие строки пишет только ручной
--      импорт, не конвейер (конвейер всегда знает complex_id).
-- sentiment/topics NULL до классификации (CHECK как в developer_reviews,
-- 'spam' включён — regex-фильтр на этапе классификации).
CREATE TABLE IF NOT EXISTS reviews_raw (
    id                SERIAL PRIMARY KEY,
    developer_id      INTEGER REFERENCES developers(id) ON DELETE SET NULL,
    complex_id        INTEGER REFERENCES complexes(id) ON DELETE CASCADE,
    source            TEXT NOT NULL CHECK (source = ANY (ARRAY['2gis', 'google_maps', 'yandex'])),
    source_entity_id  TEXT,
    author            TEXT,
    review_date       DATE,
    rating            REAL,
    review_text       TEXT NOT NULL,
    text_hash         TEXT NOT NULL,
    source_url        TEXT,
    raw               JSONB,
    sentiment         TEXT CHECK (sentiment = ANY (ARRAY['positive', 'negative', 'neutral', 'spam'])),
    topics            TEXT[],
    classified_at     TIMESTAMPTZ,
    fetched_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (complex_id, source, text_hash)
);
CREATE INDEX IF NOT EXISTS idx_reviews_raw_unclassified
    ON reviews_raw (classified_at) WHERE classified_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_reviews_raw_complex ON reviews_raw (complex_id);

GRANT SELECT, INSERT, UPDATE, DELETE ON reviews_raw TO krisha;
GRANT USAGE, SELECT ON SEQUENCE reviews_raw_id_seq TO krisha;
