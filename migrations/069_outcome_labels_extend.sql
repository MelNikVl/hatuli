-- Фаза A.5, п.4 вердикт-стратегии (docs/verdict_strategy.md, задача
-- 2026-08-14) — disappeared_within_30d один в один был прокси-ликвидности
-- ("быстрый архив без снижений"), но НЕ различал три разные причины
-- исчезновения объявления с одним и тем же наблюдаемым фактом
-- (is_active стало FALSE): реальная продажа, релист (тот же объект под
-- новым listing_id — на Krisha это обычное поведение при "поднятии"
-- объявления) и снятие модерацией (нарушение правил/дубль — тоже не
-- имеет отношения к цене/спросу). disappeared_within_30d остаётся как
-- есть (не переопределяется, чтобы не сломать уже посчитанный Фазой A
-- baseline, docs/scoring_roadmap.md Часть 6 п.3) — новые колонки уточняют
-- его, не заменяют.
--
-- clean_disappearance_within_30d — disappeared_within_30d ЗА ВЫЧЕТОМ
-- вероятного релиста/модерации: более консервативный, но более честный
-- прокси именно ликвидности рынка (см. docs/verdict_strategy.md, Фаза
-- A.5 п.7 — "disappeared_within_30d — прокси ликвидности, не продажа").
-- relisted_within_60d — строгое совпадение (тот же ЖК+комнаты+этаж+
-- похожая площадь±5%+похожая цена±15%) с НОВЫМ объявлением, появившимся
-- в течение 60 дней после архивации — высокая уверенность "это тот же
-- объект переопубликован".
-- possibly_relisted — ослабленное совпадение (ЖК+комнаты+площадь±10%,
-- без требования по этажу/цене), НЕ пойманное строгим правилом —
-- средняя уверенность, тот же принцип двух порогов (auto/review), что
-- уже применяется в bot/core/entity_resolution.py score_match().
-- possibly_moderation_removed — эвристика "снято раньше, чем система
-- успела хоть раз подтянуть детальную страницу" (archived_at-first_seen
-- <=2 дня И details_fetched НЕ TRUE) — слабый сигнал (отсюда "possibly"),
-- не факт, явно так и подписан.
-- observation_days — сколько дней объявление реально наблюдалось
-- (COALESCE(archived_at, now())-first_seen), для активных тоже
-- определено (в отличие от time_on_market, которое НЕ censored только
-- у архивных) — нужен baseline_measure.py, чтобы честно исключать
-- объявления с недостаточным окном наблюдения.
-- censored — TRUE = ещё активно, исход не финален (стандартный термин
-- survival-анализа) — прямая замена вывода "archived_at IS NULL" по
-- имени поля, не по стилю запроса каждый раз заново.
-- outcome_notes — короткое текстовое объяснение, почему флаги встали
-- так, как встали (не гадаем молча).
ALTER TABLE outcome_labels
    ADD COLUMN IF NOT EXISTS clean_disappearance_within_30d BOOLEAN,
    ADD COLUMN IF NOT EXISTS relisted_within_60d             BOOLEAN,
    ADD COLUMN IF NOT EXISTS possibly_relisted                BOOLEAN,
    ADD COLUMN IF NOT EXISTS possibly_moderation_removed      BOOLEAN,
    ADD COLUMN IF NOT EXISTS observation_days                 INT,
    ADD COLUMN IF NOT EXISTS censored                         BOOLEAN,
    ADD COLUMN IF NOT EXISTS outcome_notes                    TEXT;

CREATE INDEX IF NOT EXISTS idx_outcome_labels_clean_disappearance ON outcome_labels (clean_disappearance_within_30d);
CREATE INDEX IF NOT EXISTS idx_outcome_labels_censored ON outcome_labels (censored);
