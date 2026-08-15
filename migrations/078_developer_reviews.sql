-- developer_reviews — отзывы на ЖК с 2GIS (DeepSeek, задача 2026-08-15,
-- сбор — 2gis_reviews_collect.py). Таблица была создана ПРЯМО В БД, в
-- обход migrations/ (владелец в pg_tables — `postgres`, не `krisha`,
-- как у всех таблиц, заведённых через нормальный путь _apply_migrations()
-- в bot/db/pg.py) — тот же антипаттерн, что когда-то `air_stations`/
-- `air_quality_astana`/`air_grid` (те тоже до сих пор без миграции,
-- отдельный долг, не в этом файле). Урок: каждая таблица обязана иметь
-- миграцию — без неё свежий деплой (пустая БД) не получит эту таблицу
-- вообще, а прод было бы невозможно честно откатить/воспроизвести.
--
-- Эта миграция — CREATE TABLE IF NOT EXISTS РОВНО по живой схеме на
-- 2026-08-15 (см. \d+ developer_reviews), без добавления НОВЫХ
-- индексов, которых сейчас нет в проде (даже там, где по паттерну
-- запросов в terminal_extras.py — WHERE complex_id=$1 (/complex/{id}
-- блок отзывов), WHERE sentiment=... (/admin/developer-reviews фильтр)
-- — индекс бы пригодился: это ОТДЕЛЬНОЕ решение, не "тихая правка заодно").
-- На проде IF NOT EXISTS сделает миграцию no-op (таблица уже есть,
-- владелец не меняется — ALTER OWNER здесь не выполняется, у krisha и
-- так есть GRANT на все нужные операции, см. ниже); на свежем деплое
-- создаст таблицу как надо, с владельцем krisha.
--
-- source — источник отзыва ('2gis' на 2026-08-15, задел на другие).
-- source_entity_id — id организации в системе источника (geo_id 2GIS).
-- UNIQUE(complex_id, source_entity_id, review_text) — защита от повторной
-- записи того же отзыва при повторном прогоне коллектора (тот же ЖК +
-- та же организация 2GIS + тот же текст = тот же отзыв).
-- sentiment — CHECK на 4 значения: LLM-классификация (DeepSeek) при
-- сборе, 'spam' — regex-фильтр до LLM (см. докстринг
-- 2gis_reviews_collect.py), 'positive'/'negative'/'neutral' — обычная
-- классификация. Ручная переклассификация — /admin/developer-reviews/update.
-- developer_bin/developer_id/complex_id — привязка отзыва к застройщику/
-- ЖК; developer_id/complex_id nullable (не для каждого отзыва матчинг
-- гарантированно резолвится), developer_bin — сырой БИН с источника
-- (задел на матчинг, как в kzk_registry, миграция 074).
CREATE TABLE IF NOT EXISTS developer_reviews (
    id                SERIAL PRIMARY KEY,
    developer_bin     TEXT,
    developer_id      INTEGER REFERENCES developers(id) ON DELETE SET NULL,
    complex_id        INTEGER REFERENCES complexes(id) ON DELETE CASCADE,
    source            TEXT NOT NULL DEFAULT '2gis',
    source_entity_id  TEXT,
    review_text       TEXT,
    rating            REAL,
    sentiment         TEXT CHECK (sentiment = ANY (ARRAY['positive', 'negative', 'neutral', 'spam'])),
    topics            TEXT[],
    review_date       DATE,
    author            TEXT,
    verified          BOOLEAN DEFAULT FALSE,
    source_url        TEXT,
    fetched_at        TIMESTAMPTZ DEFAULT now(),
    UNIQUE (complex_id, source_entity_id, review_text)
);

GRANT SELECT, INSERT, UPDATE, DELETE ON developer_reviews TO krisha;
GRANT USAGE, SELECT ON SEQUENCE developer_reviews_id_seq TO krisha;
