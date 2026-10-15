-- Reviewed identity decisions survive every automatic geobind recalculation.
-- NULL canonical_id pins a record as its own physical complex.
CREATE TABLE IF NOT EXISTS complex_canonical_overrides (
    complex_id INTEGER PRIMARY KEY REFERENCES complexes(id),
    canonical_id INTEGER REFERENCES complexes(id),
    evidence JSONB NOT NULL CHECK (evidence <> '{}'::jsonb),
    reviewed_by TEXT NOT NULL CHECK (btrim(reviewed_by) <> ''),
    reviewed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (canonical_id IS NULL OR canonical_id <> complex_id)
);
CREATE TABLE IF NOT EXISTS complex_canonical_override_log (
    log_id BIGSERIAL PRIMARY KEY,
    complex_id INTEGER NOT NULL REFERENCES complexes(id),
    previous_canonical_id INTEGER REFERENCES complexes(id),
    canonical_id INTEGER REFERENCES complexes(id),
    evidence JSONB NOT NULL,
    reviewed_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE complexes DROP CONSTRAINT IF EXISTS complexes_canonical_reason_check;
ALTER TABLE complexes ADD CONSTRAINT complexes_canonical_reason_check
    CHECK (canonical_reason IS NULL OR canonical_reason = ANY (
        ARRAY['krisha_slug', 'name_prefix', 'junk_unmatched', 'manual_review']));
GRANT SELECT, INSERT, UPDATE, DELETE ON complex_canonical_overrides TO krisha;
GRANT SELECT, INSERT ON complex_canonical_override_log TO krisha;
GRANT USAGE, SELECT ON SEQUENCE complex_canonical_override_log_log_id_seq TO krisha;
