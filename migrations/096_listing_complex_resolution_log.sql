-- Complex Identity layer — provenance journal для listing -> complex_id
-- resolution attempts (задача 2026-08-30, Phase 5). Schema-only, НЕТ
-- backfill в этой миграции (задача, явно). Append-only с рождения —
-- ни один код в этом PR не делает UPDATE/DELETE сюда (тот же принцип,
-- что property_merge_log/property_match_review_log/property_merge_
-- manifest_log — см. их докстринги для прецедента этого паттерна в
-- проекте).
--
-- Один INSERT = одна ПОПЫТКА резолва listing_id -> complex_id, ЛЮБЫМ
-- resolution_method (не только будущий Tier A auto-resolve — ручное
-- решение ревьюера, эвристика, будущий resolver — все пишут сюда
-- одинаково, resolution_method различает источник). Существует
-- НЕЗАВИСИМО от факта, попал ли listing.resolved_house_id/properties.
-- complex_id в итоге в изменение — сама эта миграция НЕ трогает ни то,
-- ни другое (задача, явно: "Не трогать текущие assignments", "Никакого
-- backfill пока").
--
-- complex_name_at_resolution — СНИМОК сырого текста на момент попытки,
-- НЕ ссылка на текущее apartment_listings.complex_name (то поле живое,
-- мутирует при перескрапе — задача Phase 4 предыдущего аудита явно
-- предупредила: "не считать нынешнее имя ЖК исторической истиной для
-- старого listing без проверки"). Без этого столбца НЕВОЗМОЖНО потом
-- честно ответить "каким текстом было названо это объявление, когда МЫ
-- его резолвили" — ключевой provenance-факт для is Complex Identity
-- истории (то же соображение, что renamed_same_complex в migrations/095:
-- прошлое имя — исторический факт, не ошибка для исправления).
CREATE TABLE IF NOT EXISTS listing_complex_resolution_log (
    log_id                    SERIAL PRIMARY KEY,
    listing_id                TEXT NOT NULL REFERENCES apartment_listings(id),
    complex_id                INTEGER NOT NULL REFERENCES complexes(id),
    resolution_method         TEXT NOT NULL,
    confidence_tier           TEXT NOT NULL CHECK (confidence_tier IN ('A', 'B', 'C')),
    resolved_at                TIMESTAMPTZ NOT NULL,
    complex_name_at_resolution TEXT,
    evidence                      JSONB NOT NULL,
    resolver_version                 TEXT NOT NULL,
    created_at                          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_lcrl_listing ON listing_complex_resolution_log (listing_id);
CREATE INDEX IF NOT EXISTS idx_lcrl_complex ON listing_complex_resolution_log (complex_id);
CREATE INDEX IF NOT EXISTS idx_lcrl_confidence_tier ON listing_complex_resolution_log (confidence_tier);

-- НЕТ ON DELETE CASCADE на listing_id/complex_id (тот же принцип, что
-- migrations/095) — provenance-журнал должен пережить/заблокировать
-- случайное удаление того, что он документирует, не исчезнуть вместе
-- с ним молча.

GRANT SELECT, INSERT, UPDATE, DELETE ON listing_complex_resolution_log TO krisha;
GRANT USAGE, SELECT ON SEQUENCE listing_complex_resolution_log_log_id_seq TO krisha;
