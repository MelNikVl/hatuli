-- Property Identity — physical merge, durable provenance/audit trail
-- (задача: "Перед следующими physical merges закрыть auditability/
-- provenance gap"). Это НЕ вторая система поверх property_merge_log
-- (migrations/092) — она остаётся источником истины для repoint-фактов
-- (какой listing куда переехал). Эти таблицы отвечают на другой вопрос:
-- "чем был оправдан каждый apply, и что было проверено после него",
-- вопрос, на который read-only provenance audit (2026-08-30) не смог
-- ответить для batch20/size3-canary — manifest-файлы не сохранялись,
-- exec-лога не было, timeline-валидация не оставляла следа.
--
-- Тот же архитектурный принцип, что property_merge_log и property_match_
-- review_log: append-only, ничего не UPDATE (кроме единственного
-- допустимого случая — см. докстринг property_merge_execution_log ниже
-- про то, почему даже он пишется ОДНОЙ INSERT-строкой, не INSERT+UPDATE).

-- ── 1. Frozen manifest — persisted ПЕРЕД apply, неизменяемый снимок ──────
-- Один manifest (из plan_property_merge()) -> одна строка, ДО первого
-- вызова apply_property_merge() на этот manifest. Код (bot/identity/
-- property_merge_provenance.py::persist_manifest) никогда не делает
-- UPDATE/DELETE на эту таблицу — только INSERT. Это отдельная таблица,
-- не расширение property_merge_log: сохраняется даже для manifest'ов,
-- apply которых потом окажется blocked/stale/error (property_merge_log
-- пишется ТОЛЬКО при реальном repoint, а manifest должен быть виден в
-- audit trail независимо от исхода).
CREATE TABLE IF NOT EXISTS property_merge_manifest_log (
    manifest_id             SERIAL PRIMARY KEY,
    component_hash          TEXT NOT NULL,
    candidate_ids            JSONB NOT NULL,
    property_ids               JSONB NOT NULL,
    canonical_property_id       INTEGER NOT NULL REFERENCES properties(property_id),
    losing_property_ids            JSONB NOT NULL,
    -- {"<losing_property_id>": ["<listing_id>", ...], ...} — из manifest
    -- evidence_snapshot.moved_listing_ids, дублировано на верхний уровень
    -- для дешёвых индексируемых запросов ("какие listing ожидались для
    -- этого manifest"), сам manifest ниже хранит то же самое как часть
    -- полного снимка.
    expected_listing_ids              JSONB NOT NULL,
    warnings                             JSONB NOT NULL DEFAULT '[]'::jsonb,
    evidence_snapshot                       JSONB NOT NULL,
    -- Полный оригинальный manifest dict (plan_property_merge() -> save_
    -- manifest()) КАК ЕСТЬ, byte-for-byte-эквивалентный JSON — источник
    -- истины для component_hash-сверки на apply, не пересобирается из
    -- колонок выше (те — для запросов, не для валидации).
    manifest                                   JSONB NOT NULL,
    manifest_created_at                            TIMESTAMPTZ NOT NULL,
    persisted_at                                       TIMESTAMPTZ NOT NULL DEFAULT now(),
    tool_version                                          TEXT NOT NULL,
    actor                                                    TEXT NOT NULL,
    -- Git provenance на момент persist (см. property_merge_execution_log
    -- ниже — та же тройка полей, дублирована здесь: manifest мог быть
    -- создан заметно раньше apply, момент planning тоже стоит фиксировать).
    git_sha                                                     TEXT,
    git_branch                                                     TEXT,
    git_dirty                                                         BOOLEAN
);
CREATE INDEX IF NOT EXISTS idx_pmml_component_hash ON property_merge_manifest_log (component_hash);
CREATE INDEX IF NOT EXISTS idx_pmml_canonical ON property_merge_manifest_log (canonical_property_id);

-- ── 2. Execution audit — один apply-attempt = одна строка ────────────────
-- Пишется ОДНИМ INSERT в конце попытки (успешной, blocked ИЛИ упавшей с
-- exception, см. property_merge_provenance.py::apply_property_merge_
-- durable — try/except оборачивает вызов apply_property_merge() целиком),
-- не INSERT (started) + UPDATE (finished): started_at/finished_at — оба
-- известны к моменту единственной записи (Python-время до/после вызова),
-- UPDATE после INSERT сюда добавил бы ЕДИНСТВЕННЫЙ destructive-update
-- путь в этой миграции — намеренно исключён, задача явно просит append-
-- only. Цена: падение процесса ДО этого INSERT (например, kill -9 между
-- successful repoint-транзакцией и записью аудита) оставит repoint без
-- execution-строки — property_merge_log (источник истины для факта
-- repoint) при этом уже содержит запись независимо (своя транзакция),
-- так что сам факт merge не теряется, теряется только эта audit-запись
-- об attempt — принятый компромисс ради строгой append-only гарантии.
CREATE TABLE IF NOT EXISTS property_merge_execution_log (
    execution_id                SERIAL PRIMARY KEY,
    manifest_id                   INTEGER NOT NULL REFERENCES property_merge_manifest_log(manifest_id),
    -- Заполняется только при status='merged' — связывает эту execution-
    -- строку с N строками property_merge_log (migrations/092), которые
    -- реально описывают repoint. NULL для blocked/error/already_merged
    -- попыток (already_merged МОЖЕТ иметь merge_group_key от ПРЕДЫДУЩЕГО
    -- успешного execution — заполняется, если он был найден).
    merge_group_key                  UUID,
    status                              TEXT NOT NULL CHECK (status IN (
        'merged', 'already_merged', 'blocked_stale', 'blocked_conflict',
        'blocked_provenance', 'error'
    )),
    dry_run                                BOOLEAN NOT NULL,
    started_at                                TIMESTAMPTZ NOT NULL,
    finished_at                                  TIMESTAMPTZ NOT NULL,
    manifest_hash                                   TEXT NOT NULL,
    -- [{"listing_id":..., "from_property_id":..., "to_property_id":...}]
    -- — пусто для любого исхода, кроме 'merged'.
    rows_repointed                                     JSONB,
    property_statuses_before                              JSONB,
    property_statuses_after                                  JSONB,
    error                                                        TEXT,
    -- Полный сырой dict, который вернул apply_property_merge()/исключение
    -- — на случай, если колонки выше не покрывают что-то нужное разбору
    -- инцидента постфактум (те же соображения, что decision_source в
    -- property_merge_log).
    result_detail                                                   JSONB NOT NULL,
    actor                                                               TEXT NOT NULL,
    git_sha                                                                TEXT,
    git_branch                                                                TEXT,
    git_dirty                                                                    BOOLEAN,
    provenance_override                                                            BOOLEAN NOT NULL DEFAULT FALSE,
    provenance_override_reason                                                        TEXT
);
CREATE INDEX IF NOT EXISTS idx_pmel_manifest ON property_merge_execution_log (manifest_id);
CREATE INDEX IF NOT EXISTS idx_pmel_group ON property_merge_execution_log (merge_group_key);
CREATE INDEX IF NOT EXISTS idx_pmel_status ON property_merge_execution_log (status);

-- ── 3. Timeline validation result — read-only checks после успешного apply
-- validate_property_merge(execution_id) вызывает build_property_timeline()
-- и статические проверки (см. bot/identity/property_merge_provenance.py),
-- НИКОГДА не откатывает сам (задача, явно: "Не делать auto-rollback внутри
-- validator") — только персистит passed/failed + разбивку по каждому
-- check'у. execution_id обязателен (NOT NULL + FK) — валидация БЕЗ
-- реального execution-attempt не персистится вообще (read-only demo-
-- прогоны на legacy-данных используют ту же логику проверок как чистую
-- функцию, но результат не пишут сюда — см. scripts/audit_property_
-- merge_provenance_dry_run.py, чтобы не создавать fake-впечатление
-- "тогда была валидация", которой не было).
CREATE TABLE IF NOT EXISTS property_merge_validation_log (
    validation_id           SERIAL PRIMARY KEY,
    execution_id               INTEGER NOT NULL REFERENCES property_merge_execution_log(execution_id),
    canonical_property_id         INTEGER NOT NULL REFERENCES properties(property_id),
    passed                           BOOLEAN NOT NULL,
    -- [{"name": "...", "passed": bool, "detail": "..."}]
    checks                              JSONB NOT NULL,
    validated_at                           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_pmvl_execution ON property_merge_validation_log (execution_id);
CREATE INDEX IF NOT EXISTS idx_pmvl_canonical ON property_merge_validation_log (canonical_property_id);

-- ── 4. Provenance notes — явно помеченные reconstructed-факты о ПРОШЛЫХ
-- (до этой миграции) merge-операциях, которые не могут иметь настоящую
-- manifest/execution/validation строку выше (их не существовало тогда).
-- Append-only с рождения (никакой код в этом PR не делает UPDATE/DELETE
-- сюда). is_reconstructed=TRUE — ЕДИНСТВЕННОЕ, что сюда когда-либо
-- писалось этим PR (задача, явно: "не пытаться дорисовать как будто их
-- audit существовал тогда" — is_reconstructed=FALSE зарезервировано для
-- будущих операций, где note пишется ЖИВЬЁМ в момент события, не задним
-- числом; этот PR такую строку не создаёт ни разу).
CREATE TABLE IF NOT EXISTS property_merge_provenance_note (
    note_id             SERIAL PRIMARY KEY,
    merge_group_key        UUID NOT NULL,
    note_type                  TEXT NOT NULL,
    -- Свободная форма (в частности note_type='legacy_provenance_incomplete'
    -- — снимок read-only audit находок: git SHA bracketing через reflog,
    -- отсутствие manifest-файлов на диске, интервалы между execute_at,
    -- то, что удалось и не удалось восстановить).
    detail                        JSONB NOT NULL,
    is_reconstructed                 BOOLEAN NOT NULL DEFAULT TRUE,
    created_at                          TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by                             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pmpn_group ON property_merge_provenance_note (merge_group_key);

GRANT SELECT, INSERT, UPDATE, DELETE ON property_merge_manifest_log TO krisha;
GRANT USAGE, SELECT ON SEQUENCE property_merge_manifest_log_manifest_id_seq TO krisha;
GRANT SELECT, INSERT, UPDATE, DELETE ON property_merge_execution_log TO krisha;
GRANT USAGE, SELECT ON SEQUENCE property_merge_execution_log_execution_id_seq TO krisha;
GRANT SELECT, INSERT, UPDATE, DELETE ON property_merge_validation_log TO krisha;
GRANT USAGE, SELECT ON SEQUENCE property_merge_validation_log_validation_id_seq TO krisha;
GRANT SELECT, INSERT, UPDATE, DELETE ON property_merge_provenance_note TO krisha;
GRANT USAGE, SELECT ON SEQUENCE property_merge_provenance_note_note_id_seq TO krisha;
