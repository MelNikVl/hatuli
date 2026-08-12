-- Entity resolution, фаза 1 (ЖК) — см. docs/entity_resolution_plan.md.
-- complexes.id остаётся единственным entity_id (PK, без семантики).
-- code — отдельный человеко-читаемый мутируемый идентификатор формата
-- JK-000123, без привязки к источнику/застройщику (те ребрендятся,
-- транслит плодит дубли — семантика живёт в атрибутах, не в ключе).
ALTER TABLE complexes ADD COLUMN IF NOT EXISTS code TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_complexes_code ON complexes (code) WHERE code IS NOT NULL;

-- Один ЖК (entity) <-> много карточек на источниках. Заменяет однослотовые
-- complexes.krisha_url/korter_url/newbuild_source(_id) как основной способ
-- записи новых связей (старые колонки остаются read-through кэшем ещё
-- один цикл — не убираем сразу, см. план).
CREATE TABLE IF NOT EXISTS complex_source_links (
    id            SERIAL PRIMARY KEY,
    complex_id    INT NOT NULL REFERENCES complexes(id) ON DELETE CASCADE,
    source        TEXT NOT NULL,       -- 'krisha' | 'korter' | 'homsters' | 'homeportal' |
                                        -- 'bi_group' | 'nak' | 'orda_invest' | 'bazis' |
                                        -- 'sensata' | 'svoydom' | 'qadam' | ...
    source_id     TEXT NOT NULL,       -- id/slug объекта в системе источника
    url           TEXT,
    match_method  TEXT NOT NULL,       -- 'seed_source' | 'name_exact' | 'name_exact+geo' |
                                        -- 'name_exact+geo+developer' | 'manual' (см. entity_resolution.py)
    confidence    NUMERIC,             -- 0..1, NULL для manual (де-факто 1.0)
    matched_at    TIMESTAMPTZ DEFAULT now(),
    matched_by    TEXT,                -- 'auto' | admin username | 'backfill_2026-08-12'
    UNIQUE (source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_complex_source_links_complex ON complex_source_links (complex_id);
CREATE INDEX IF NOT EXISTS idx_complex_source_links_matched_at ON complex_source_links (matched_at);
