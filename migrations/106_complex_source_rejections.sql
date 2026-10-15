-- A proven wrong source URL must not return on the next enrichment cycle.
CREATE TABLE IF NOT EXISTS complex_source_rejections (
    complex_id INTEGER NOT NULL REFERENCES complexes(id),
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    evidence JSONB NOT NULL CHECK (evidence <> '{}'::jsonb),
    reviewed_by TEXT NOT NULL,
    reviewed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(complex_id,source,source_id)
);
GRANT SELECT, INSERT, UPDATE, DELETE ON complex_source_rejections TO krisha;
