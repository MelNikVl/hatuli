-- Opaque, revocable operator sessions; browser cookies carry only random tokens.
CREATE TABLE IF NOT EXISTS admin_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES admin_users(id) ON DELETE CASCADE,
    credential_hash TEXT NOT NULL,
    csrf_token TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_admin_sessions_user_id ON admin_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_admin_sessions_expires_at ON admin_sessions(expires_at);

-- This remains consumed even if an old Telegram callback changes status again.
ALTER TABLE login_tokens ADD COLUMN IF NOT EXISTS consumed_at TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS full_access BOOLEAN DEFAULT FALSE;
