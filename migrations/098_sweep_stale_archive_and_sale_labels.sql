-- 1) archive_reason='sweep_stale' (задача 2026-10-03): массовая архивация по
--    last_seen — объявление не появлялось в выдаче Крыши N дней при живом
--    deep sweep (bot/core/stale_archive.py). Раньше is_active=TRUE висело у
--    ~107K строк при ~19K живых: archive_check подтверждает по HTTP ~40
--    объявлений за цикл и физически не успевает (docs/phase_c_liquidity_report.md §1).
--    archived_at = last_seen (оценка даты ухода, а не дата нашей проверки) —
--    иначе в статистике «ушло сегодня» был бы всплеск на десятки тысяч.
ALTER TABLE apartment_listings DROP CONSTRAINT IF EXISTS apartment_listings_archive_reason_check;
ALTER TABLE apartment_listings ADD CONSTRAINT apartment_listings_archive_reason_check
    CHECK (archive_reason IS NULL OR archive_reason = ANY (ARRAY['confirmed_gone', 'archived_badge', 'sweep_stale']));

-- 2) Метки исхода «продано / снято / перевыставлено» (задача 2026-10-03) —
--    вход для будущей модели P(продажа): по Крыше продажу от снятия не
--    отличить (страница архива одинакова, "status":"archive"), нужна разметка.
--    manual — человек; heuristic_relist — авто: та же квартира появилась под
--    новым id (bot/core/sale_labels.py). Ручная метка всегда главнее авто.
CREATE TABLE IF NOT EXISTS sale_outcome_labels (
    listing_id   TEXT PRIMARY KEY REFERENCES apartment_listings(id),
    label        TEXT NOT NULL CHECK (label IN ('sold', 'withdrawn', 'relisted', 'unknown')),
    source       TEXT NOT NULL CHECK (source IN ('manual', 'heuristic_relist')),
    relisted_as  TEXT,
    labeled_by   TEXT,
    note         TEXT,
    labeled_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_sale_outcome_labels_label ON sale_outcome_labels (label, source);
