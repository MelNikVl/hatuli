-- Фаза A, п.2 вердикт-стратегии (docs/verdict_strategy.md §5) — outcome-
-- метки из уже имеющейся истории (price_history/archived_at) + ongoing
-- пополнение (views_history, Г2, живёт только с 2026-08-14).
--
-- Это НЕ event-лог (не append-only) — вычисляемые признаки на объявление,
-- пересчитываются периодически по мере того, как разрешаются (NULL —
-- "ещё рано отвечать", не "неизвестно навсегда"), поэтому по
-- temporal_policy.md правилу 2 — computed_at (когда последний раз
-- ПЕРЕСЧИТАНО), не observed_at (это не сырое собранное значение).
-- UPSERT по listing_id, а не история версий — старое значение метки не
-- нужно хранить, нужен только текущий (актуальный на сегодня) вердикт
-- по каждому объявлению.
--
-- Все bool-поля — NULLABLE ТРОИЧНО (TRUE/FALSE/NULL): NULL = "исход ещё
-- не наступил и окно не закрылось" ИЛИ "нет достоверных данных для
-- честного ответа" (например price_history не покрывает нужный период —
-- см. outcome_labels_backfill.py) — принцип "Unknown ≠ average" из §3.1
-- verdict_strategy.md применяется и здесь: не гадаем, помечаем NULL.
--
-- disappeared_within_30d — "быстрый архив без снижений" (см. живую
-- формулировку в прежней версии scoring_roadmap.md, замещённую этим же
-- документом): TRUE только если объявление ушло в архив В ПРЕДЕЛАХ
-- 30 дней от first_seen И за это время НЕ было ни одного снижения цены.
-- price_reduction_within_30d — отдельная, независимая метка: была ли
-- хоть одна корректировка цены вниз в первые 30 дней (независимо от
-- архивации).
CREATE TABLE IF NOT EXISTS outcome_labels (
    listing_id                  TEXT PRIMARY KEY REFERENCES apartment_listings(id) ON DELETE CASCADE,
    disappeared_within_30d      BOOLEAN,
    price_reduction_within_30d  BOOLEAN,
    survives_90d                BOOLEAN,
    time_on_market               INT,   -- дни first_seen→archived_at; NULL = censored (ещё активно)
    views_velocity               NUMERIC, -- просмотров/день между первым и последним наблюдением views_history
    computed_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_outcome_labels_disappeared ON outcome_labels (disappeared_within_30d);
CREATE INDEX IF NOT EXISTS idx_outcome_labels_survives ON outcome_labels (survives_90d);

GRANT SELECT, INSERT, UPDATE, DELETE ON outcome_labels TO krisha;
