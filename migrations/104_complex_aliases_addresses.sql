-- Preserve all observed spellings/addresses independently of the identity ID.
-- Observed values are evidence, not an automatic approval to merge projects.
-- One normalized name may belong to several complexes; callers must reject
-- ambiguous keys rather than pick the first row.
CREATE OR REPLACE FUNCTION complex_name_key(value TEXT)
RETURNS TEXT LANGUAGE SQL IMMUTABLE PARALLEL SAFE AS $$
    -- Python lower expands capital dotted I; PostgreSQL's simple lower
    -- otherwise loses its dot and could equate distinct source spellings.
    SELECT lower(replace(btrim(regexp_replace(
        regexp_replace(
            -- PostgreSQL \s depends on locale; explicitly mirror Python's
            -- Unicode whitespace set, including NBSP and narrow NBSP.
            regexp_replace(btrim(regexp_replace(COALESCE(value, ''),
                           U&'[\0009-\000D\001C-\0020\0085\00A0\1680\2000-\200A\2028\2029\202F\205F\3000]+', ' ', 'g')),
                           '^(жк|кг) *["«]?', '', 'i'),
            '[«»"''“”]', '', 'g'),
        ' +', ' ', 'g')), U&'\0130', U&'i\0307'))
$$;
CREATE INDEX IF NOT EXISTS idx_complexes_name_key ON complexes (complex_name_key(name));

CREATE TABLE IF NOT EXISTS complex_aliases (
    alias_id        SERIAL PRIMARY KEY,
    complex_id      INTEGER NOT NULL REFERENCES complexes(id),
    name            TEXT NOT NULL CHECK (btrim(name) <> ''),
    normalized_name TEXT NOT NULL CHECK (normalized_name <> '' AND normalized_name = complex_name_key(name)),
    source          TEXT NOT NULL,
    source_id       TEXT NOT NULL,
    evidence        JSONB NOT NULL DEFAULT '{}'::jsonb,
    status          TEXT NOT NULL DEFAULT 'observed'
                    CHECK (status IN ('observed', 'verified', 'rejected')),
    reviewed_by     TEXT,
    reviewed_at     TIMESTAMPTZ,
    first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (complex_id, source, source_id, name)
);
CREATE INDEX IF NOT EXISTS idx_complex_aliases_name_verified
    ON complex_aliases (normalized_name, complex_id) WHERE status = 'verified';
CREATE INDEX IF NOT EXISTS idx_complex_aliases_complex ON complex_aliases (complex_id);

CREATE TABLE IF NOT EXISTS complex_addresses (
    address_id          SERIAL PRIMARY KEY,
    complex_id          INTEGER NOT NULL REFERENCES complexes(id),
    address             TEXT NOT NULL CHECK (btrim(address) <> ''),
    normalized_address  TEXT NOT NULL CHECK (normalized_address <> ''),
    city                TEXT,
    street_key          TEXT,
    house_number        TEXT,
    building_complex_id INTEGER REFERENCES complexes(id),
    source              TEXT NOT NULL,
    source_id           TEXT NOT NULL,
    evidence            JSONB NOT NULL DEFAULT '{}'::jsonb,
    status              TEXT NOT NULL DEFAULT 'observed'
                        CHECK (status IN ('observed', 'verified', 'rejected')),
    reviewed_by         TEXT,
    reviewed_at         TIMESTAMPTZ,
    first_seen_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (complex_id, source, source_id, address)
);
CREATE INDEX IF NOT EXISTS idx_complex_addresses_complex ON complex_addresses (complex_id);
CREATE INDEX IF NOT EXISTS idx_complex_addresses_verified
    ON complex_addresses (city, street_key, house_number) WHERE status = 'verified';

GRANT SELECT, INSERT, UPDATE, DELETE ON complex_aliases, complex_addresses TO krisha;
GRANT USAGE, SELECT ON SEQUENCE complex_aliases_alias_id_seq, complex_addresses_address_id_seq TO krisha;
