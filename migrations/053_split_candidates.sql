-- "Расшивка" (unravel) как review-очередь, задача 2026-08-13 —
-- зеркало unravel_blobs.py (см. docs/entity_resolution_plan.md,
-- "политика гранулярности ЖК"), но источник сигнала другой: там —
-- homeportal_objects (адрес объекта против адреса ЖК), тут —
-- apartment_listings.complex_name (явный токен очереди/блока в ИМЕНИ,
-- который Крыша сама уже развела по разным строкам complex_name, см.
-- живой кейс "Rio De Janeiro" / "Rio De Janeiro 3") + расхождение
-- адреса. Тот же паттерн очереди, что unit_duplicate_candidates/
-- complex_duplicate_candidates: status/evidence/matched_by/resolved_*.
--
-- complex_id — кандидат-"блоб" (комплекс, который подозревается в том,
-- что реально описывает несколько разных зданий/очередей под одним
-- именем) — НЕ пара id, в отличие от complex_duplicate_candidates:
-- расшивка создаёт НОВЫЙ complex_id при исполнении, тут его ещё нет.
CREATE TABLE IF NOT EXISTS split_candidates (
    id              SERIAL PRIMARY KEY,
    complex_id      INT NOT NULL REFERENCES complexes(id) ON DELETE CASCADE,
    reason          TEXT NOT NULL,   -- 'manual' | 'explicit_token_address' | 'address_diverge_no_token'
    comment         TEXT,            -- комментарий человека (кнопка "пометить на расшивку")
    evidence        JSONB,           -- {tokens, addresses, sample listing ids, ...} — детектор или ручная пометка
    matched_by      TEXT NOT NULL,   -- 'admin'/логин (ручная пометка) | 'split_detect_2026-08-13' (детектор)
    status          TEXT NOT NULL DEFAULT 'review',  -- 'review' | 'approved' | 'rejected'
    created_at      TIMESTAMPTZ DEFAULT now(),
    resolved_at     TIMESTAMPTZ,
    resolved_by     TEXT
);
CREATE INDEX IF NOT EXISTS idx_split_candidates_status ON split_candidates (status);
CREATE INDEX IF NOT EXISTS idx_split_candidates_complex ON split_candidates (complex_id);

GRANT SELECT, INSERT, UPDATE, DELETE ON split_candidates TO krisha;
GRANT USAGE, SELECT ON SEQUENCE split_candidates_id_seq TO krisha;
