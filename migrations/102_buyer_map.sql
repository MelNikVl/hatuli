-- Selected 100 m cells and short-lived cross-process onboarding draft.
-- Existing profiles and radius filters remain valid; no separate user table.
ALTER TABLE users ADD COLUMN IF NOT EXISTS buyer_map JSONB NOT NULL DEFAULT '{}'::jsonb;
