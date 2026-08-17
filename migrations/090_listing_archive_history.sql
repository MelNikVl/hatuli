-- Задача 2026-08-17 (follow-up на fix(archive-check): idempotent
-- archived_at, reactivate reappeared listings): "История прежней
-- архивации не должна теряться. Если archived_at/archive_reason
-- очищаются, добавь append-only историю статусов".
--
-- bot.core.archive_check.reactivate_reappeared_listings() при реактивации
-- очищает apartment_listings.archived_at/archive_reason (они по контракту
-- остального кода означают "ТЕКУЩИЙ период архивации", NULL = "сейчас не
-- архивно" — тот же контракт, что уже был ДО этой задачи, менять его
-- ради истории не будем, is_active/archived_at/archive_reason остаются
-- "текущее состояние"). Один завершённый цикл архивация->реактивация —
-- одна строка здесь: archived_at/archive_reason — СТАРЫЕ значения (что
-- было ДО очистки), reactivated_at — когда обнаружена реактивация.
CREATE TABLE IF NOT EXISTS listing_archive_history (
    id              SERIAL PRIMARY KEY,
    listing_id      TEXT NOT NULL REFERENCES apartment_listings(id) ON DELETE CASCADE,
    archived_at     TIMESTAMPTZ NOT NULL,   -- начало ЭТОГО периода архивации (старое apartment_listings.archived_at)
    archive_reason  TEXT,                    -- 'confirmed_gone' | 'archived_badge', см. migrations/089
    reactivated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_listing_archive_history_listing ON listing_archive_history (listing_id);
CREATE INDEX IF NOT EXISTS idx_listing_archive_history_reactivated_at ON listing_archive_history (reactivated_at);

GRANT SELECT, INSERT, UPDATE, DELETE ON listing_archive_history TO krisha;
GRANT USAGE, SELECT ON SEQUENCE listing_archive_history_id_seq TO krisha;
