-- Evidence инлайн для review-очереди (задача "очередь кандидатов",
-- 2026-08-13, см. docs/entity_resolution_plan.md) — match_method уже
-- кодирует, КАКИЕ сигналы сработали ("name_fuzzy(0.75)+geo+developer"),
-- но не численные дельты (гео в метрах, чем именно отличается имя).
-- evidence — тот же паттерн, что complex_duplicate_candidates.evidence
-- (migrations/047), сюда — geo_m/same_developer/address_match/name_sim
-- при записи кандидата (record_source_link()), не постфактум.
ALTER TABLE complex_source_link_candidates ADD COLUMN IF NOT EXISTS evidence JSONB;
