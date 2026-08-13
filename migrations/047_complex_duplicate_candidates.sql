-- Транслит-дубли complexes (задача гейта 2, п.5→след., см.
-- docs/entity_resolution_plan.md) — sweep_translit_dups.py нашёл 199
-- групп complexes с одинаковым именем в двух алфавитах (Tandau/Тандау —
-- первый найденный и слитый вручную; остальные — на очереди). Пары,
-- прошедшие критерий auto-мерджа (гео<=150м ИЛИ тот же застройщик ИЛИ
-- address_match), мерджатся сразу (merge_translit_dups.py); пары БЕЗ
-- подтверждающего сигнала (только совпадение имени) или с продуктовым
-- префиксом на одной стороне (Highvill-пенальти) — сюда, на ручной
-- разбор. Отдельная таблица от complex_source_link_candidates —
-- там пара (source, source_id, complex_id), тут пара (complex_id,
-- complex_id), другая природа кандидата (дубль сущности, не привязка
-- источника).
CREATE TABLE IF NOT EXISTS complex_duplicate_candidates (
    id              SERIAL PRIMARY KEY,
    complex_id_a    INT NOT NULL REFERENCES complexes(id) ON DELETE CASCADE,
    complex_id_b    INT NOT NULL REFERENCES complexes(id) ON DELETE CASCADE,
    translit_key    TEXT NOT NULL,
    reason          TEXT NOT NULL,   -- 'no_confirming_signal' | 'product_token_mismatch'
    evidence        JSONB,           -- {geo_m, same_developer, address_match, product_a, product_b, ...}
    method          TEXT NOT NULL DEFAULT 'translit_sweep_2026-08-12',
    status          TEXT NOT NULL DEFAULT 'review',  -- 'review' | 'merged' | 'rejected'
    created_at      TIMESTAMPTZ DEFAULT now(),
    resolved_at     TIMESTAMPTZ,
    resolved_by     TEXT,
    UNIQUE (complex_id_a, complex_id_b)
);
CREATE INDEX IF NOT EXISTS idx_cdc_status ON complex_duplicate_candidates (status);

GRANT SELECT, INSERT, UPDATE, DELETE ON complex_duplicate_candidates TO krisha;
GRANT USAGE, SELECT ON SEQUENCE complex_duplicate_candidates_id_seq TO krisha;
