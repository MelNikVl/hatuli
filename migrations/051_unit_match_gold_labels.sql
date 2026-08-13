-- Фаза 2 (юниты) — gold labels для будущей перекалибровки (задача
-- 2026-08-13, "review-driven режим" по прямому решению заказчика, см.
-- docs/entity_resolution_plan.md). Отдельно от unit_duplicate_candidates
-- .status: candidates живут операционно (approve мержит запись в
-- unit_source_links, reject просто помечает статус, skip вообще не
-- трогает статус — "посмотрел, не решил, вернусь"). Эта таблица —
-- append-only журнал решений человека С evidence-снимком на МОМЕНТ
-- решения (не текущим — evidence кандидата может сдвинуться при
-- будущем перескоре, тот же паттерн, что rescore_review_queue.py для
-- complex-уровня) — сырьё для будущей калибровки порогов/весов
-- unit-matching, не операционное состояние очереди.
CREATE TABLE IF NOT EXISTS unit_match_gold_labels (
    id              SERIAL PRIMARY KEY,
    candidate_id    INT REFERENCES unit_duplicate_candidates(id) ON DELETE SET NULL,
    unit_id         INT NOT NULL,
    listing_id      TEXT NOT NULL,
    complex_id      INT,
    reason          TEXT,             -- reason кандидата на момент решения (no_confirmation | ambiguous_floorplan)
    decision        TEXT NOT NULL,    -- 'approve' | 'reject' | 'skip'
    evidence        JSONB,            -- снимок evidence кандидата на момент решения
    decided_by      TEXT NOT NULL,
    decided_at      TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_umgl_candidate ON unit_match_gold_labels (candidate_id);
CREATE INDEX IF NOT EXISTS idx_umgl_decision ON unit_match_gold_labels (decision);

GRANT SELECT, INSERT, UPDATE, DELETE ON unit_match_gold_labels TO krisha;
GRANT USAGE, SELECT ON SEQUENCE unit_match_gold_labels_id_seq TO krisha;
