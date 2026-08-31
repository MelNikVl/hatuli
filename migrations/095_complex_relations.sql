-- Complex Identity layer — human-reviewed relation facts between two
-- `complexes` rows (задача 2026-08-30, "Complex Identity layer", Phase 4).
-- Schema-only migration — НЕТ data backfill в этой миграции (задача,
-- явно). Ничего не пишет сюда автоматически; только человек, через
-- будущий review-процесс (см. scripts/build_complex_relation_review_
-- dataset.py — готовит candidate-датасет, НЕ пишет в эту таблицу).
--
-- ── Почему НЕ 'ambiguous' в CHECK ────────────────────────────────────
-- Задача явно: "ambiguous лучше не хранить как факт relation, а
-- держать как review status/candidate state" — 'ambiguous' здесь
-- ПРИНЦИПИАЛЬНО отсутствует в списке допустимых relation_type. Пара
-- БЕЗ строки в этой таблице == "ещё не рассмотрено или рассмотрено и
-- признано неоднозначным" — отсутствие факта, не отдельный факт.
--
-- ── Почему duplicate_same_complex И renamed_same_complex — РАЗНЫЕ ───
-- (задача, явно, п. "стратегически") — duplicate: две ошибочно заведённые
-- записи одного и того же ЖК СЕЙЧАС (typically confirmed через прямое
-- property-пересечение — Property Identity видела ОДНУ физическую
-- квартиру под обоими complex_id). renamed_same_complex: ЖК реально
-- менял название/бренд ВО ВРЕМЕНИ (напр. "Sardar Compass" -> позже тот
-- же объект как "Бурабай") — это часть ИСТОРИИ идентичности ЖК для
-- Hatuli, не просто data hygiene: старый listing честно назывался
-- иначе на момент своего наблюдения, "исправление" задним числом на
-- текущее имя стёрло бы реальный исторический факт.
--
-- ── Canonical ordering / uniqueness / append-update semantics ────────
-- complex_id_a < complex_id_b ВСЕГДА (CHECK) — один неупорядоченный pair
-- физически представим ровно одной строкой, не двумя зеркальными.
-- UNIQUE(complex_id_a, complex_id_b) БЕЗ relation_type в ключе (не
-- "UNIQUE по pair+relation_type") — сознательно: у пары ЖК есть РОВНО
-- ОДНО актуальное отношение единовременно (не может быть одновременно
-- "duplicate" И "separate_neighbor" — противоречие), не набор фактов.
-- ЗНАЧИТ: relation_id -> UPDATE (не новый INSERT) — стандартный путь
-- ИСПРАВЛЕНИЯ существующей записи (напр. reviewer передумал/уточнил
-- confidence). Полная история исправлений НЕ хранится этой таблицей
-- намеренно (минимальный foundation, задача явно просила МИНИМАЛЬНУЮ
-- схему) — если понадобится full audit-trail изменений самой разметки,
-- это отдельное решение поверх (напр. таблица complex_relations_history
-- по тому же append-only паттерну, что property_merge_log), НЕ здесь.
CREATE TABLE IF NOT EXISTS complex_relations (
    relation_id       SERIAL PRIMARY KEY,
    complex_id_a      INTEGER NOT NULL REFERENCES complexes(id),
    complex_id_b      INTEGER NOT NULL REFERENCES complexes(id),
    relation_type     TEXT NOT NULL CHECK (relation_type IN (
        'duplicate_same_complex', 'sibling_phase', 'same_umbrella_project',
        'renamed_same_complex', 'separate_neighbor_complex'
    )),
    -- 0.0-1.0, human-присвоенная (не автоматически посчитанный score) —
    -- насколько reviewer уверен в relation_type выше.
    confidence        NUMERIC NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    evidence          JSONB NOT NULL,
    -- Каждая строка — РЕЗУЛЬТАТ человеческого решения, не сырое
    -- предложение аудит-скрипта (то живёт вне БД, в JSON review-датасете,
    -- см. модульный докстринг скрипта выше) — поэтому reviewed_by/
    -- reviewed_at NOT NULL, не nullable "пока не рассмотрено".
    reviewed_by       TEXT NOT NULL,
    reviewed_at       TIMESTAMPTZ NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Версия методологии/промпта/скрипта, которым была ПРЕДЛОЖЕНА эта
    -- пара человеку на рассмотрение (напр. "bigville_audit_v1",
    -- "sibling_audit_v1") — не версия самого relation_type решения
    -- (то — reviewed_by/reviewed_at, человеческое, не версionируемое).
    methodology_version TEXT NOT NULL,

    CONSTRAINT complex_relations_canonical_order CHECK (complex_id_a < complex_id_b),
    CONSTRAINT complex_relations_pair_unique UNIQUE (complex_id_a, complex_id_b)
);
CREATE INDEX IF NOT EXISTS idx_complex_relations_a ON complex_relations (complex_id_a);
CREATE INDEX IF NOT EXISTS idx_complex_relations_b ON complex_relations (complex_id_b);
CREATE INDEX IF NOT EXISTS idx_complex_relations_type ON complex_relations (relation_type);

-- НЕТ ON DELETE CASCADE на complex_id_a/b (задача, явно: "никаких cascade
-- deletes") — удаление complexes-строки, на которую ссылается уже
-- проверенное relation, ЗАБЛОКИРОВАНО (default RESTRICT/NO ACTION) —
-- сначала нужно осознанно разобраться с relation, не потерять факт
-- молча вместе с удалением записи.

GRANT SELECT, INSERT, UPDATE, DELETE ON complex_relations TO krisha;
GRANT USAGE, SELECT ON SEQUENCE complex_relations_relation_id_seq TO krisha;
