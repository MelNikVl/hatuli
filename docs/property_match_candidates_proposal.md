# property_match_candidates — предложение схемы (НЕ ПРИМЕНЕНО)

Задача 2026-08-16, "безопасный deterministic exact-only property linker",
п.4-5. **Это ПРЕДЛОЖЕНИЕ, не миграция** — намеренно НЕ лежит в
`migrations/` (файлы там подхватываются `bot/db/pg.py::_apply_migrations()`
автоматически на каждый `init_pool()`, независимо от git-статуса —
положить туда `.sql` означало бы применить его на следующий тестовый
прогон без явного ОК). Применяется только после явного решения
пользователя — тогда копируется в `migrations/086_....sql` и нумеруется
по факту (086 может быть уже занят к тому моменту другой задачей).

## Контекст

`match_mode="exact_only"` (см. `bot/identity/property_linker.py`) больше
не связывает fuzzy-кандидатов автоматически — но продолжает их
**вычислять** и возвращать в `result["fuzzy_candidate"]` (задача, п.4:
"fuzzy-кандидат можно залогировать как candidate, но он не должен
мешать созданию отдельного property"). Сейчас эта информация нигде не
сохраняется — видна только внутри одного вызова `link_listing_to_
property()`. `property_match_candidates` — предлагаемое постоянное
хранилище для будущей ручной проверки/ML-дообучения, БЕЗ автоматической
записи в `property_listings`.

## Предлагаемая схема

```sql
-- ПРЕДЛОЖЕНИЕ — НЕ ПРИМЕНЯТЬ без явного ОК пользователя (задача
-- 2026-08-16, "безопасный exact-only property linker", п.4-5).
CREATE TABLE IF NOT EXISTS property_match_candidates (
    id                  SERIAL PRIMARY KEY,
    listing_id          TEXT NOT NULL REFERENCES apartment_listings(id) ON DELETE CASCADE,
    candidate_property_id INTEGER REFERENCES properties(property_id) ON DELETE CASCADE,
    -- NULL — кандидат существовал только в рамках ОДНОГО dry-run прогона
    -- (DryRunCache, "created" ещё не вставленной property) — тот же
    -- случай, что fuzzy_candidate["candidate_property_id"]=None в
    -- bot/identity/property_linker.py.
    match_method        TEXT NOT NULL DEFAULT 'fuzzy',  -- пока единственный источник кандидатов
    confidence           NUMERIC NOT NULL,
    evidence              JSONB,  -- {"area_diff": 0.8, "complex_id": ..., "floor": ..., "cache_only": bool}
    matcher_version        TEXT NOT NULL,  -- см. ниже — версия правил, которыми найден кандидат
    status                    TEXT NOT NULL DEFAULT 'pending',  -- 'pending'|'accepted'|'rejected'
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_at                   TIMESTAMPTZ,
    reviewed_by                     TEXT,
    UNIQUE (listing_id, candidate_property_id)
);
CREATE INDEX IF NOT EXISTS idx_pmc_status ON property_match_candidates (status);
CREATE INDEX IF NOT EXISTS idx_pmc_listing ON property_match_candidates (listing_id);

GRANT SELECT, INSERT, UPDATE, DELETE ON property_match_candidates TO krisha;
GRANT USAGE, SELECT ON SEQUENCE property_match_candidates_id_seq TO krisha;
```

Паттерн (naming/shape) сознательно повторяет уже существующие в проекте
`unit_duplicate_candidates`/`unit_source_links` (`migrations/049`/`050`,
другая пара сущностей — `newbuild_units`↔`apartment_listings`, не
пересекается с этой задачей, но задаёт согласованный стиль:
`evidence JSONB`, `confidence NUMERIC`, `status`, `reviewed_at`).

`status='pending'` до ручного решения; `'accepted'` — человек подтвердил,
что это та же квартира (тогда отдельным явным шагом, НЕ автоматически,
`property_listings` дополняется реальной связью); `'rejected'` —
подтверждено, что это разные квартиры (кандидат больше не предлагается).

## Проверка property_listings — задача, п.5

Схема (`migrations/084_property_listings.sql`) УЖЕ содержит:

| запрошено в задаче | есть? | реальная колонка |
|---|---|---|
| match_method | ✅ (другое имя) | `link_method` (`'auto'\|'manual'\|'fuzzy'` — CHECK constraint) |
| confidence | ✅ | `confidence NUMERIC NOT NULL DEFAULT 1.0` |
| linked_at | ✅ | `linked_at TIMESTAMPTZ NOT NULL DEFAULT now()` |
| matcher_version | ❌ | отсутствует |

## Предлагаемая минимальная миграция (matcher_version)

```sql
-- ПРЕДЛОЖЕНИЕ — НЕ ПРИМЕНЯТЬ без явного ОК пользователя.
ALTER TABLE property_listings ADD COLUMN IF NOT EXISTS matcher_version TEXT;
```

**Обоснование**: `link_method`/`confidence` фиксируют ЧТО решил линковщик,
но не ЧЕМ именно (какой версией правил) — если правила fuzzy/exact-only
поменяются в будущем (например, tolerance с ±1м² на ±0.5м², или вариант
с проверкой номера дома — см. `scripts/audit_property_linker_fuzzy.py`
п.5, правила A-F), не будет способа отличить связи, сделанные СТАРОЙ
логикой, от НОВОЙ, без этой колонки — при пересмотре/аудите задним
числом придётся гадать. `TEXT`, не enum — версия правил будет
меняться чаще, чем стоит городить под неё отдельный тип; предлагаемое
значение сейчас — константа вида `"exact_only_v1"` в `bot/identity/
property_linker.py`, проставляемая в INSERT `property_listings` рядом с
`link_method`.

**Не применял ни эту, ни `property_match_candidates` миграцию** —
только предложение, как и просила задача.
