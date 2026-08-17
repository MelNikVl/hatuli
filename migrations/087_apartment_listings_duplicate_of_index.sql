-- Индекс на apartment_listings.duplicate_of (задача 2026-08-16, "последняя
-- проверка детерминированности candidate graph перед production
-- backfill") — НАЙДЕНО эмпирически при full-scale прогоне двухфазного
-- backfill'а (bot/identity/property_linker.py::generate_all_candidates,
-- фаза B): обратный dedup-поиск ("кто ссылается duplicate_of на МЕНЯ",
-- _find_all_dedup_partners) без индекса — Seq Scan на 50k+ строк,
-- ~40мс на КАЖДЫЙ вызов (EXPLAIN ANALYZE подтвердил), и фаза B вызывает
-- его на КАЖДЫЙ забутстрапленный listing (не только на тех, у кого
-- duplicate_of=IS NOT NULL сам — обратное направление ищет ВСЕХ, кто
-- ссылается НА него, не наоборот) — единственная причина, по которой
-- full-scale dry-run фазы B заметно медленнее однопроходного legacy-кода
-- (тот вызывал ту же нерайндексированную SELECT реже, но тоже страдал
-- бы от неё в production при реальном объёме).
--
-- Partial (WHERE duplicate_of IS NOT NULL) — сама колонка NULL у
-- большинства строк (не дубль ничего), индексировать NULL-значения
-- бессмысленно и раздувает индекс без пользы ни одному запросу этого
-- модуля (запросы всегда ищут конкретный НЕ-NULL id).
CREATE INDEX IF NOT EXISTS idx_apartment_listings_duplicate_of
    ON apartment_listings (duplicate_of) WHERE duplicate_of IS NOT NULL;
