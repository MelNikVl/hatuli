-- Property Identity — bootstrap-инфраструктура кандидатов (задача
-- 2026-08-16, "безопасная инфраструктура кандидатов" — прямое
-- продолжение read-only аудитов: scripts/audit_property_linker_fuzzy.py
-- нашёл 76.9% high-risk fuzzy + order-dependency дефект;
-- scripts/audit_address_hash_exact.py нашёл 204 exact-hash кластера с
-- ДОКАЗАННЫМ rooms mismatch (address_hash НЕ гарантирует физическую
-- квартиру — нет apartment_number/complex_id/rooms в самом хэше);
-- scripts/audit_property_match_signals.py — даже "безопасный"
-- exact-only даёт 4.3% доказанных rejected-пар. Принцип задачи: false
-- positive merge хуже false negative duplicate.
--
-- 1) UNIQUE(address_hash) СНЯТ. address_hash — это факт "адрес+этаж+
-- площадь совпадают", не факт "это одна физическая квартира" (см.
-- docs/property_identity_v2_architecture_audit.md §2, §6-7) — UNIQUE
-- структурно ЗАПРЕЩАЛ существование двух properties с одинаковым
-- хэшем, даже когда это ДВЕ РЕАЛЬНЫЕ разные квартиры одного этажа
-- многоподъездного ЖК с повторяющейся планировкой (живой пример —
-- кластер size=28, 19 разных seller identity, тот же
-- адрес+этаж+площадь+ЖК). Обычный индекс остаётся — быстрый поиск
-- "кандидаты с таким же адресом+этажом+площадью" для candidate-
-- generation НЕ теряет скорость.
ALTER TABLE properties DROP CONSTRAINT IF EXISTS properties_address_hash_key;
CREATE INDEX IF NOT EXISTS idx_properties_address_hash ON properties (address_hash);

-- 2) identity_status — состояние САМОЙ property (не путать с confidence
-- ниже, задача п.4: "не смешивать"):
--   provisional — только что bootstrap'нута из ОДНОГО listing'а,
--     ничьё совпадение с ней ещё не подтверждено человеком/сильным
--     независимым сигналом;
--   confirmed — независимая сильная валидация (ручная проверка,
--     unit_source_links, номер квартиры с обеих сторон, и т.п.) —
--     задача этого PR НЕ проставляет 'confirmed' автоматически нигде
--     (нет ground truth ещё, см. docs/property_identity_v2_
--     architecture_audit.md §4 — "confirmed" сознательно не
--     используется без него);
--   merged — эта property "проиграла" merge (её listing'и переехали в
--     другую property) — historical, НЕ удаляется физически (property_
--     merge_log — задача СЛЕДУЮЩЕГО PR, сознательно не в этом).
ALTER TABLE properties ADD COLUMN IF NOT EXISTS identity_status TEXT NOT NULL DEFAULT 'provisional';
ALTER TABLE properties DROP CONSTRAINT IF EXISTS properties_identity_status_check;
ALTER TABLE properties ADD CONSTRAINT properties_identity_status_check
    CHECK (identity_status = ANY (ARRAY['provisional', 'confirmed', 'merged']));
CREATE INDEX IF NOT EXISTS idx_properties_identity_status ON properties (identity_status);

-- 3) matcher_version — версия правил, которыми СДЕЛАНА конкретная
-- связь property_listings (не property_match_candidates.matcher_version
-- ниже — та про версию правил, которыми НАЙДЕН кандидат, до решения).
-- TEXT, не enum — версия будет меняться чаще, чем стоит городить под
-- неё отдельный тип (то же обоснование, что уже было в
-- docs/property_match_candidates_proposal.md, предыдущая задача).
ALTER TABLE property_listings ADD COLUMN IF NOT EXISTS matcher_version TEXT;

-- link_method CHECK расширяется: 'bootstrap' — задача, п.2, "listing→
-- новая provisional property: deterministic bootstrap" — НЕ 'auto'
-- (тот означал "совпадение с существующей property", bootstrap
-- означает прямо противоположное — "новая, ни с чем не сопоставлена").
ALTER TABLE property_listings DROP CONSTRAINT IF EXISTS property_listings_link_method_check;
ALTER TABLE property_listings ADD CONSTRAINT property_listings_link_method_check
    CHECK (link_method = ANY (ARRAY['auto', 'manual', 'fuzzy', 'bootstrap']));

-- 4) property_match_candidates — по архитектурному паттерну
-- unit_duplicate_candidates (migrations/049) — тот же паттерн уже был
-- предложен в docs/property_match_candidates_proposal.md (задача
-- "безопасный exact-only property linker"), сейчас применяется.
--
-- evidence JSONB — схема ГОТОВА под будущий perceptual photo-matching
-- (задача, мид-ту: "perceptual image matching пока можно оставить
-- следующим PR, но схема evidence должна его поддерживать") — в ЭТОМ
-- PR photo-сигнал НЕ вычисляется (URL фотографий сознательно НЕ
-- используется как идентификатор — предыдущий аудит нашёл 0 реальных
-- совпадений по URL/UUID на всей базе, содержательного сигнала там
-- нет), evidence несёт нейтральный плейсхолдер
-- {"photo_signal": {"method": "not_implemented", ...}}, ключи
-- shared_rare_photo_count/shared_common_photo_count зарезервированы
-- (rare — сильный позитивный сигнал concurrent_duplicate; common —
-- рендеры/планировки/фасады ЖК, встречающиеся у многих listings,
-- слабый сигнал/шум — см. bot/identity/property_linker.py докстринг).
--
-- Каноническая модель — candidate_property_id, НЕ candidate_listing_id
-- (обоснование, задача явно просит выбрать): в bootstrap-режиме
-- КАЖДЫЙ listing уже имеет РОВНО одну (свою собственную) property на
-- момент candidate-генерации — значит "этот listing похож на ТУ property"
-- содержательнее и долговечнее, чем "похож на ТОТ listing": (а)
-- унифицирует все три источника кандидатов (exact-hash/fuzzy оба
-- естественно ищут по properties, не по listings; dedup_listings.
-- duplicate_of — единственный listing-ориентированный сигнал — легко
-- резолвится в property_id через property_listings, т.к. на момент
-- генерации 1:1); (б) остаётся валидным после будущих merge/accept
-- (property_id — долгоживущий identity anchor, listing_id "той
-- стороны" мог бы устареть, если ЕГО property потом смержат в другую);
-- (в) тот же выбор, что unit_duplicate_candidates.unit_id (не listing_id
-- на обеих сторонах) — согласованный паттерн проекта.
-- relationship_type — ОТДЕЛЬНО от status (задача, мид-ту-уточнение
-- 2026-08-16, "simultaneous activity НЕ безусловный конфликт"): одна
-- физическая квартира может ОДНОВРЕМЕННО продаваться через несколько
-- агентов с разными listing_id/ценами/описаниями (concurrent_duplicate)
-- — это НЕ "разные квартиры", хотя пересекается по времени активности
-- (сигнал, который раньше в этой же ветке задач трактовался как
-- негативный — здесь сознательно пересмотрено: пересечение активности
-- само по себе НЕ понижает и НЕ отклоняет кандидата, только выбирает
-- между 'concurrent_duplicate' и 'possible_same_property'/'relist' при
-- прочих равных, см. bot/identity/property_linker.py::classify_
-- relationship). 'unknown' — дефолт, когда сигналов недостаточно
-- определить даже примерный характер связи.
CREATE TABLE IF NOT EXISTS property_match_candidates (
    candidate_id            SERIAL PRIMARY KEY,
    listing_id              TEXT NOT NULL REFERENCES apartment_listings(id) ON DELETE CASCADE,
    candidate_property_id   INTEGER NOT NULL REFERENCES properties(property_id) ON DELETE CASCADE,
    match_method            TEXT NOT NULL,   -- 'exact_hash' | 'fuzzy' | 'dedup_listings'
    match_score             NUMERIC NOT NULL,
    relationship_type       TEXT NOT NULL DEFAULT 'unknown',
    evidence                JSONB,
    conflict_reasons        JSONB,
    matcher_version         TEXT NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'pending',
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_at             TIMESTAMPTZ,
    reviewed_by             TEXT,
    UNIQUE (listing_id, candidate_property_id)
);
ALTER TABLE property_match_candidates ADD CONSTRAINT property_match_candidates_method_check
    CHECK (match_method = ANY (ARRAY['exact_hash', 'fuzzy', 'dedup_listings']));
ALTER TABLE property_match_candidates ADD CONSTRAINT property_match_candidates_status_check
    CHECK (status = ANY (ARRAY['pending', 'accepted', 'rejected']));
ALTER TABLE property_match_candidates ADD CONSTRAINT property_match_candidates_relationship_check
    CHECK (relationship_type = ANY (ARRAY['concurrent_duplicate', 'relist', 'possible_same_property', 'unknown']));
CREATE INDEX IF NOT EXISTS idx_pmc_status ON property_match_candidates (status);
CREATE INDEX IF NOT EXISTS idx_pmc_listing ON property_match_candidates (listing_id);
CREATE INDEX IF NOT EXISTS idx_pmc_candidate_property ON property_match_candidates (candidate_property_id);
CREATE INDEX IF NOT EXISTS idx_pmc_relationship_type ON property_match_candidates (relationship_type);

GRANT SELECT, INSERT, UPDATE, DELETE ON property_match_candidates TO krisha;
-- PK-колонка называется candidate_id (не id) -> auto-сгенерированная
-- последовательность candidate_id_seq, не id_seq (найдено на первом же
-- прогоне на чистой БД).
GRANT USAGE, SELECT ON SEQUENCE property_match_candidates_candidate_id_seq TO krisha;

-- Сознательно НЕ в этой миграции (задача, явно): properties.
-- newbuild_unit_id, property_merge_log — следующий PR.
