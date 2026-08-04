-- Изменения источников-агрегаторов (korter/homsters): новые ЖК и изменённые
-- поля относительно предыдущего прогона. Читается вкладками Korter/Homsters
-- страницы /admin/parsers.
CREATE TABLE IF NOT EXISTS source_changes (
    id           SERIAL PRIMARY KEY,
    source       TEXT NOT NULL,          -- 'korter' | 'homsters' | ...
    complex_id   INTEGER,
    complex_name TEXT NOT NULL,
    change_type  TEXT NOT NULL,          -- 'new' | 'updated'
    field        TEXT,                   -- для 'updated' — имя поля
    old_value    TEXT,
    new_value    TEXT,
    ts           TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_source_changes_src_ts ON source_changes (source, ts DESC);

-- Прогоны источников: длительность полного обхода и счётчики.
CREATE TABLE IF NOT EXISTS source_runs (
    id         SERIAL PRIMARY KEY,
    source     TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    duration_s NUMERIC,
    matched    INTEGER,
    created    INTEGER,
    changed    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_source_runs_src ON source_runs (source, started_at DESC);
