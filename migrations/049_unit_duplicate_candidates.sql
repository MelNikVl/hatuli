-- Фаза 2 (юниты), гейт п.Б — задача 2026-08-13, см.
-- docs/entity_resolution_plan.md ("numbers-first проход — реальные
-- 28 ЖК"). Дубль-кандидаты между newbuild_units (застройщик) и
-- apartment_listings (Крыша, is_new_build=TRUE) — тот же паттерн, что
-- complex_duplicate_candidates (migrations/047), другая природа пары
-- (unit_id INT <-> listing_id TEXT, не два complex_id).
CREATE TABLE IF NOT EXISTS unit_duplicate_candidates (
    id              SERIAL PRIMARY KEY,
    unit_id         INT NOT NULL REFERENCES newbuild_units(id) ON DELETE CASCADE,
    listing_id      TEXT NOT NULL REFERENCES apartment_listings(id) ON DELETE CASCADE,
    complex_id      INT NOT NULL REFERENCES complexes(id) ON DELETE CASCADE,
    reason          TEXT NOT NULL,   -- 'no_confirmation' | 'ambiguous_floorplan' (зеркальный кап)
    evidence        JSONB,           -- {floor_match, area_delta, price_delta_pct, date_overlap, unit_number_nb, unit_number_al, mirror_count_nb, mirror_count_al}
    method          TEXT NOT NULL DEFAULT 'unit_match_2026-08-13',
    status          TEXT NOT NULL DEFAULT 'review',  -- 'review' | 'merged' | 'rejected'
    created_at      TIMESTAMPTZ DEFAULT now(),
    resolved_at     TIMESTAMPTZ,
    resolved_by     TEXT,
    UNIQUE (unit_id, listing_id)
);
CREATE INDEX IF NOT EXISTS idx_udc_status ON unit_duplicate_candidates (status);
CREATE INDEX IF NOT EXISTS idx_udc_complex ON unit_duplicate_candidates (complex_id);

-- auto-разрешённые пары идут НЕ сюда — в unit_source_links
-- (migrations/050, тот же spine-паттерн, что complex_source_links),
-- apartment_listings не гарбейджится (не дубль-строка, живёт своей
-- жизнью — цена/статус своя аналитика).

GRANT SELECT, INSERT, UPDATE, DELETE ON unit_duplicate_candidates TO krisha;
GRANT USAGE, SELECT ON SEQUENCE unit_duplicate_candidates_id_seq TO krisha;
