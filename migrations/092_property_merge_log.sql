-- Property Identity — physical merge, append-only audit journal (задача
-- 2026-08-20, "Safe Physical Property Merge"). Схема — ровно та, что
-- спроектирована и обсуждена в docs/property_merge_design.md §2 (тот же
-- документ резервировал это имя как "следующий PR" ещё в migrations/086
-- докстринге) — миграция не изобретает вторую схему, переносит design doc
-- в SQL как есть.
--
-- Тот же архитектурный паттерн, что property_match_review_log
-- (migrations/088): append-only, ничего не UPDATE кроме rolled_back_at/
-- rollback_reason (откат ДОБАВЛЯЕТ факт, не стирает строку — см. §5).
--
-- Одна строка = одна losing_property_id, репойнтнутая в canonical в
-- рамках ОДНОЙ merge-операции. Операция группы из N properties -> N-1
-- строк с общим merge_group_key (canonical сам в себя не мерджится).
CREATE TABLE IF NOT EXISTS property_merge_log (
    merge_id               SERIAL PRIMARY KEY,
    merge_group_key        UUID NOT NULL,
    canonical_property_id  INTEGER NOT NULL REFERENCES properties(property_id),
    losing_property_id     INTEGER NOT NULL REFERENCES properties(property_id),
    -- Снимок listing_id, реально перенесённых ИМЕННО в рамках losing_
    -- property_id -> canonical (JSONB массив TEXT, не FK) — property_
    -- listings живёт своей жизнью дальше, снимок остаётся неизменным
    -- историческим фактом (design doc §2).
    moved_listing_ids      JSONB NOT NULL,
    -- {"candidate_ids": [...], "review_log_ids": [...]} — из каких
    -- property_match_candidates/property_match_review_log строк ЭТА
    -- losing_property_id была включена в merge-группу.
    decision_source        JSONB NOT NULL,
    matcher_version         TEXT NOT NULL,
    merge_tool_version       TEXT NOT NULL,
    dry_run                   BOOLEAN NOT NULL DEFAULT FALSE,
    executed_by                TEXT NOT NULL,
    executed_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    rolled_back_at                TIMESTAMPTZ,
    rollback_reason                 TEXT
);
CREATE INDEX IF NOT EXISTS idx_pml_group ON property_merge_log (merge_group_key);
CREATE INDEX IF NOT EXISTS idx_pml_canonical ON property_merge_log (canonical_property_id);
CREATE INDEX IF NOT EXISTS idx_pml_losing ON property_merge_log (losing_property_id);
-- Быстрый "ещё не откачено" фильтр — rollback_property_merge и будущая
-- страница аудита фильтруют по этому чаще, чем по всей таблице.
CREATE INDEX IF NOT EXISTS idx_pml_not_rolled_back ON property_merge_log (merge_group_key)
    WHERE rolled_back_at IS NULL;

GRANT SELECT, INSERT, UPDATE, DELETE ON property_merge_log TO krisha;
GRANT USAGE, SELECT ON SEQUENCE property_merge_log_merge_id_seq TO krisha;
