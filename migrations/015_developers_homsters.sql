-- ============================================================
-- 015: Застройщики — интеграция с homsters.kz/developers.
--
-- Колонки founded_year, website, projects_*, score_* и др. в своё
-- время были добавлены на проде вручную (как и сами базовые таблицы
-- до миграции 000) и никогда не попали в SQL-файлы. Здесь они
-- добавляются идемпотентно для воспроизводимости с чистой базы.
-- Новое: homsters_slug (стабильный идентификатор застройщика на
-- homsters.kz) и description (текст со страницы застройщика).
-- ============================================================

ALTER TABLE developers ADD COLUMN IF NOT EXISTS founded_year       INTEGER;
ALTER TABLE developers ADD COLUMN IF NOT EXISTS website            TEXT;
ALTER TABLE developers ADD COLUMN IF NOT EXISTS projects_total     INTEGER DEFAULT 0;
ALTER TABLE developers ADD COLUMN IF NOT EXISTS projects_delivered INTEGER DEFAULT 0;
ALTER TABLE developers ADD COLUMN IF NOT EXISTS projects_delayed   INTEGER DEFAULT 0;
ALTER TABLE developers ADD COLUMN IF NOT EXISTS projects_active    INTEGER DEFAULT 0;
ALTER TABLE developers ADD COLUMN IF NOT EXISTS avg_delay_months   REAL;
ALTER TABLE developers ADD COLUMN IF NOT EXISTS has_court_cases    BOOLEAN DEFAULT FALSE;
ALTER TABLE developers ADD COLUMN IF NOT EXISTS court_cases_count  INTEGER DEFAULT 0;
ALTER TABLE developers ADD COLUMN IF NOT EXISTS score_total        INTEGER;
ALTER TABLE developers ADD COLUMN IF NOT EXISTS score_delivery     INTEGER;
ALTER TABLE developers ADD COLUMN IF NOT EXISTS score_quality      INTEGER;
ALTER TABLE developers ADD COLUMN IF NOT EXISTS score_financial    INTEGER;
ALTER TABLE developers ADD COLUMN IF NOT EXISTS updated_at         TIMESTAMPTZ DEFAULT now();

ALTER TABLE developers ADD COLUMN IF NOT EXISTS homsters_slug      TEXT;
ALTER TABLE developers ADD COLUMN IF NOT EXISTS description        TEXT;

-- Слаг уникален среди заполненных (NULL'ы не конфликтуют)
CREATE UNIQUE INDEX IF NOT EXISTS idx_developers_homsters_slug
    ON developers (homsters_slug) WHERE homsters_slug IS NOT NULL;
