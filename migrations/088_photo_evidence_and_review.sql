-- Photo evidence + manual property match review (задача 2026-08-17,
-- "Property Identity — photo evidence + admin review queue", прямое
-- продолжение migrations/086: её evidence JSONB уже резервировал ключ
-- "photo_signal" именно под эту задачу — "не реализовывать автоматический
-- photo merge в текущем PR, но не закладывать в схему ошибочное правило".
-- НИКАКОГО автоматического merge и здесь: эти таблицы только копят
-- evidence и ручные решения, property_listings/properties не трогают.

-- 1) Per-photo fingerprint cache — задача B: "Для каждой фотографии
-- кандидатов рассчитывать и кэшировать SHA-256/perceptual hash/embedding/
-- тип". listing_id — ВСЕ фотографии объявления (не только те, что попали
-- в candidate-пары) кэшируются один раз, переиспользуются для ЛЮБОГО
-- числа candidate-пар этого listing'а — не пересчитываем дважды одну и
-- ту же фотографию для двух разных пар.
--
-- Две отдельные "стадии готовности": fetch_status (скачано+sha256+phash —
-- main venv, bot/identity/photo_evidence.py + scripts/photo_evidence_
-- scan.py, БЕЗ torch) и embedding/photo_type (AI-стадия — ТОЛЬКО
-- /home/nik/floorplan-clip/venv, scripts/photo_evidence_ai_scan.py,
-- тот же backbone, что floorplan_scan.py уже использует для планировок —
-- НЕ новый парсер/новая модель с нуля). embedding NULL, пока AI-стадия не
-- отработала — это НОРМАЛЬНОЕ промежуточное состояние, не ошибка.
CREATE TABLE IF NOT EXISTS listing_photo_fingerprints (
    id              SERIAL PRIMARY KEY,
    listing_id      TEXT NOT NULL REFERENCES apartment_listings(id) ON DELETE CASCADE,
    photo_url       TEXT NOT NULL,
    sha256          TEXT,                 -- SHA-256 фактического содержимого файла (не URL)
    phash           TEXT,                 -- imagehash.phash (bot.core.dedup.compute_image_hash — переиспользован)
    embedding       BYTEA,                -- packed float32[dim], NULL до AI-стадии
    embedding_model TEXT,                 -- напр. 'siglip_vit_b16_webli_v1', NULL до AI-стадии
    photo_type      TEXT,                 -- interior|view|floorplan|building_common|render|other, NULL до AI-стадии
    fetch_status    TEXT NOT NULL DEFAULT 'pending',
    fetch_error     TEXT,
    computed_at     TIMESTAMPTZ,          -- sha256/phash посчитаны
    ai_computed_at  TIMESTAMPTZ,          -- embedding/photo_type посчитаны
    UNIQUE (listing_id, photo_url)
);
ALTER TABLE listing_photo_fingerprints ADD CONSTRAINT lpf_fetch_status_check
    CHECK (fetch_status = ANY (ARRAY['pending', 'ok', 'download_failed', 'decode_failed']));
ALTER TABLE listing_photo_fingerprints ADD CONSTRAINT lpf_photo_type_check
    CHECK (photo_type IS NULL OR photo_type = ANY (
        ARRAY['interior', 'view', 'floorplan', 'building_common', 'render', 'other']));
CREATE INDEX IF NOT EXISTS idx_lpf_listing ON listing_photo_fingerprints (listing_id);
CREATE INDEX IF NOT EXISTS idx_lpf_sha256 ON listing_photo_fingerprints (sha256);
CREATE INDEX IF NOT EXISTS idx_lpf_pending_ai ON listing_photo_fingerprints (id)
    WHERE fetch_status = 'ok' AND embedding IS NULL;

GRANT SELECT, INSERT, UPDATE, DELETE ON listing_photo_fingerprints TO krisha;
GRANT USAGE, SELECT ON SEQUENCE listing_photo_fingerprints_id_seq TO krisha;

-- 2) Per-candidate-pair aggregated photo evidence — задача B, второй
-- список полей. candidate_id PK (1:1 с property_match_candidates) — "не
-- выполнять глобальное попарное сравнение... сравнивать только внутри
-- уже найденных пар-кандидатов" ЗНАЧИТ ровно одна строка evidence на
-- candidate, не на (listing, listing)-пару вообще (candidate_id уже
-- уникально идентифицирует такую пару, см. property_match_candidates.
-- UNIQUE(listing_id, candidate_property_id) в migrations/086).
--
-- model_version — задача, "гейты": "модель и пороги обязательно
-- версионировать" — комбинация версии подсчёта хешей/агрегации И
-- версии embedding-модели (см. bot/identity/photo_evidence.py
-- _EVIDENCE_VERSION/_EMBEDDING_MODEL_VERSION), меняется при изменении
-- порогов/логики — пересчёт затем идемпотентно перезаписывает строку
-- (ON CONFLICT DO UPDATE, см. save_candidate_evidence), не копит историю.
CREATE TABLE IF NOT EXISTS property_candidate_photo_evidence (
    candidate_id                INTEGER PRIMARY KEY
                                 REFERENCES property_match_candidates(candidate_id) ON DELETE CASCADE,
    photo_count_a                INTEGER NOT NULL DEFAULT 0,
    photo_count_b                INTEGER NOT NULL DEFAULT 0,
    exact_shared_count           INTEGER NOT NULL DEFAULT 0,
    perceptual_shared_count      INTEGER NOT NULL DEFAULT 0,
    ai_similar_count             INTEGER NOT NULL DEFAULT 0,
    shared_unit_specific_count   INTEGER NOT NULL DEFAULT 0,  -- interior/view с ОБЕИХ сторон — сильный сигнал
    shared_common_count          INTEGER NOT NULL DEFAULT 0,  -- floorplan/render/building/other — слабый
    max_similarity                REAL,
    matched_photos                JSONB,   -- [{a_url,b_url,method,similarity,type_a,type_b}, ...] (top-N)
    model_version                 TEXT NOT NULL,
    processing_status             TEXT NOT NULL DEFAULT 'pending',
    processing_error              TEXT,
    computed_at                   TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE property_candidate_photo_evidence ADD CONSTRAINT pcpe_status_check
    CHECK (processing_status = ANY (ARRAY['pending', 'ok', 'partial', 'error']));
CREATE INDEX IF NOT EXISTS idx_pcpe_status ON property_candidate_photo_evidence (processing_status);
CREATE INDEX IF NOT EXISTS idx_pcpe_unit_specific ON property_candidate_photo_evidence (shared_unit_specific_count)
    WHERE shared_unit_specific_count > 0;

GRANT SELECT, INSERT, UPDATE, DELETE ON property_candidate_photo_evidence TO krisha;

-- 3) Manual review decision log — задача E: "Решение записывать с
-- reviewed_at/reviewed_by/comment/matcher_version/evidence snapshot".
-- ОТДЕЛЬНАЯ append-only таблица (не только UPDATE property_match_
-- candidates.status/reviewed_at/reviewed_by, хотя это ТОЖЕ делаем, см.
-- bot/identity/review_decisions.py) — тот же паттерн, что unit_match_
-- gold_labels (migrations/051): skip НЕ меняет status кандидата (снова
-- всплывёт в очереди), но решение "посмотрел, не уверен" всё равно
-- должно остаться в журнале — если бы skip писался только в status,
-- он был бы неотличим от "ещё не смотрели" и терялся бы навсегда.
--
-- "Одна квартира" в ЭТОМ PR = decision='accepted' ЗАПИСЫВАЕТСЯ, но
-- property_listings/properties НЕ меняются (задача, явно: "физический
-- merge — отдельно, после проверки журнала решений и merge/split
-- rollback"). matcher_version — версия САМОГО candidate'а на момент
-- решения (снимок property_match_candidates.matcher_version), НЕ версия
-- review UI — под неё отдельного поля не заводим, review UI логики
-- дешёвой, версия матчинга важнее для будущей калибровки/gold-набора.
CREATE TABLE IF NOT EXISTS property_match_review_log (
    id                     SERIAL PRIMARY KEY,
    candidate_id           INTEGER NOT NULL REFERENCES property_match_candidates(candidate_id) ON DELETE CASCADE,
    listing_id             TEXT NOT NULL,
    candidate_property_id  INTEGER NOT NULL,
    decision                TEXT NOT NULL,
    comment                 TEXT,
    matcher_version          TEXT NOT NULL,
    evidence_snapshot        JSONB,
    reviewed_by              TEXT NOT NULL,
    reviewed_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE property_match_review_log ADD CONSTRAINT pmrl_decision_check
    CHECK (decision = ANY (ARRAY['accepted', 'rejected', 'skip']));
CREATE INDEX IF NOT EXISTS idx_pmrl_candidate ON property_match_review_log (candidate_id);
CREATE INDEX IF NOT EXISTS idx_pmrl_reviewed_at ON property_match_review_log (reviewed_at);

GRANT SELECT, INSERT, UPDATE, DELETE ON property_match_review_log TO krisha;
GRANT USAGE, SELECT ON SEQUENCE property_match_review_log_id_seq TO krisha;

-- Сознательно НЕ в этой миграции: ALTER на property_match_candidates.
-- match_method CHECK — corroborating_methods (задача C) живёт ВНУТРИ
-- evidence JSONB (уже готовый JSONB-контейнер с migrations/086, ключ
-- "corroborating_methods" + "corroboration_version"), не как отдельная
-- строка/новое значение match_method — задача явно: "не создавать
-- несколько строк на одну пару, одна пара — одна candidate row".
