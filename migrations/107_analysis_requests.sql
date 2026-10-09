-- Manual buyer requests are private. Submission never starts an analysis or notification.
CREATE TABLE IF NOT EXISTS analysis_requests (
    request_id UUID PRIMARY KEY,
    listing_id TEXT NOT NULL CHECK (listing_id ~ '^[0-9]{5,20}$'),
    listing_url TEXT NOT NULL CHECK (char_length(listing_url) <= 512),
    contact TEXT NOT NULL CHECK (char_length(contact) BETWEEN 5 AND 254),
    contact_type TEXT NOT NULL CHECK (contact_type IN ('telegram', 'email')),
    contact_key TEXT NOT NULL CHECK (char_length(contact_key) = 64),
    client_key TEXT NOT NULL CHECK (char_length(client_key) = 64),
    comment TEXT NOT NULL DEFAULT '' CHECK (char_length(comment) <= 1000),
    site_user_id BIGINT REFERENCES users(user_id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'reviewing', 'done', 'rejected')),
    private_note TEXT NOT NULL DEFAULT '' CHECK (char_length(private_note) <= 2000),
    reviewed_by TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_analysis_requests_active_unique
    ON analysis_requests (listing_id, contact_key) WHERE status IN ('new', 'reviewing');
CREATE INDEX IF NOT EXISTS idx_analysis_requests_queue ON analysis_requests (status, created_at);
CREATE INDEX IF NOT EXISTS idx_analysis_requests_client ON analysis_requests (client_key, created_at);
CREATE INDEX IF NOT EXISTS idx_analysis_requests_contact ON analysis_requests (contact_key, created_at);
GRANT SELECT, INSERT, UPDATE, DELETE ON analysis_requests TO krisha;
